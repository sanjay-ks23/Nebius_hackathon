"""The StopProof pipeline: Nemotron proposes -> orchestrator validates -> sandbox executes -> verifier decides.

Verdicts in the evidence bundle come exclusively from `verifier.verify` over
traces returned by the sandbox. Model output only ever appears as proposals.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any
from collections.abc import Callable

from . import agent, analysis, patching
from .model import ModelError, Nemotron
from .runtime.races import WINDOWS
from .sandbox import SandboxUnavailable
from .tools import PolicyDenied, RunResult, SandboxRunner, TestRunError, Tools
from .verifier import Outcome

log = logging.getLogger("stopproof.pipeline")

MAX_TEST_ATTEMPTS = 3
MAX_REPAIR_ATTEMPTS = 4
EXPECTED_ERRORS = (ModelError, SandboxUnavailable, patching.Rejected, TestRunError, PolicyDenied, ValueError, FileNotFoundError)


class Pipeline:
    def __init__(self, repo: Path, model: Nemotron, sandbox: SandboxRunner, out_dir: Path = Path("runs"), on_event: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.repo, self.model, self.sandbox, self.out_dir, self.on_event = repo, model, sandbox, out_dir, on_event
        self.stages: list[dict[str, Any]] = []
        self.tools: Tools | None = None

    def _stage(self, name: str, status: str, **detail: Any) -> None:
        entry = {"stage": name, "status": status, **detail}
        self.stages.append(entry)
        log.info("stage run=%s name=%s status=%s", self.tools.run_id if self.tools else "-", name, status)
        if self.on_event:
            self.on_event(entry)

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        evidence: dict[str, Any] = {"schema": 1, "stages": self.stages}
        try:
            snapshot = analysis.load_snapshot(self.repo)
            self.tools = Tools(snapshot, self.sandbox)
            evidence.update(run_id=self.tools.run_id, repository={"path": self.repo.name, "digest": snapshot.digest, "files": sorted(snapshot.files)},
                            execution={"backend": getattr(self.sandbox, "backend", "unknown"), "label": getattr(self.sandbox, "label", "unknown")})
            log.info("pipeline_start run=%s repo=%s", self.tools.run_id, self.repo.name)
            self._execute(evidence)
        except EXPECTED_ERRORS as exc:
            stage = self.stages[-1]["stage"] if self.stages else "load"
            self._stage(stage, "error", error=f"{type(exc).__name__}: {exc}")
            evidence["error"] = {"stage": stage, "type": type(exc).__name__, "message": str(exc)}
            evidence.setdefault("run_id", f"run-failed-{int(time.time())}")
            evidence.setdefault("verdict", {"outcome": Outcome.CANNOT_ESTABLISH.value, "reason": f"pipeline failed at stage {stage!r}: {exc}"})
        evidence["audit"] = self.tools.audit if self.tools else []
        evidence["metrics"] = self._metrics(started)
        evidence["summary"] = summarize(evidence)
        self._write(evidence)
        return evidence

    def _execute(self, evidence: dict[str, Any]) -> None:
        tools = self.tools
        assert tools is not None
        self._stage("analyze", "running")
        found = agent.analyze(self.model, tools)
        evidence["analysis"] = {"proposal": found, "scanner": {k: tools.scan_result[k] for k in ("handoffs", "stop_controls", "workers", "sinks", "entrypoints")},
                                "protected_files": sorted(tools.protected), "patchable_files": sorted(tools.allowed)}
        self._stage("analyze", "done", handoff=f"{found['handoff']['file']}:{found['handoff']['symbol']}", stop=f"{found['stop_control']['file']}:{found['stop_control']['symbol']}",
                    sink=f"{found['protected_sink']['file']}:{found['protected_sink']['symbol']}")

        tools.enter_stage("test")
        baseline = self._baseline(found, evidence)
        evidence["baseline"] = baseline.to_dict()
        evidence["workflow_smoke"] = tools.call("run_workflow")
        self._stage("baseline", "done", outcome=baseline.outcome.value, windows={w["window"]: w["verdict"]["outcome"] for w in baseline.windows})

        after: RunResult | None = None
        evidence["patch"] = {"attempted": False, "applied": False, "attempts": []}
        if baseline.outcome is Outcome.STILL_ABLE_TO_ACT:
            after = self._repair(found, baseline, evidence)
        final = after if after is not None and evidence["patch"]["applied"] else baseline
        evidence["after_patch"] = after.to_dict() if after else None
        evidence["same_test_verified"] = after is not None and tools.locked is not None and after.test_sha256 == baseline.test_sha256 == tools.locked.sha256
        deciding = next(w for w in final.windows if w["verdict"]["outcome"] == final.outcome.value)
        evidence["verdict"] = {"outcome": final.outcome.value, "source": "deterministic verifier over sandbox traces", "run": final.label,
                               "window": deciding["window"], "reason": deciding["verdict"]["reason"]}
        self._stage("verdict", "done", outcome=final.outcome.value)

    def _baseline(self, found: dict[str, Any], evidence: dict[str, Any]) -> RunResult:
        tools = self.tools
        assert tools is not None
        feedback = ""
        for attempt in range(1, MAX_TEST_ATTEMPTS + 1):
            self._stage("generate_test", "running", attempt=attempt)
            source = agent.design_test(self.model, tools, found, feedback, attempt)
            try:
                locked = tools.call("generate_test", source=source)
            except patching.Rejected as exc:
                feedback = f"rejected by static validation: {exc}"
                self._stage("generate_test", "rejected", attempt=attempt, reason=str(exc))
                continue
            evidence["test"] = {"path": locked.path, "sha256": locked.sha256, "source": locked.source, "attempts": attempt,
                                "race_windows": [{"name": w.name, "description": w.description, "stop_after_event": w.stop_after} for w in WINDOWS]}
            self._stage("generate_test", "locked", sha256=locked.sha256, attempt=attempt)
            self._stage("baseline", "running")
            try:
                return tools.call("run_test")
            except TestRunError as exc:
                feedback = str(exc)
                self._stage("baseline", "test_error", attempt=attempt, reason=feedback[:300])
        raise TestRunError(f"no runnable race test after {MAX_TEST_ATTEMPTS} attempts: {feedback[:300]}")

    def _repair(self, found: dict[str, Any], baseline: RunResult, evidence: dict[str, Any]) -> RunResult | None:
        tools = self.tools
        assert tools is not None
        tools.enter_stage("repair")
        patch = evidence["patch"]
        patch["attempted"] = True
        digest = {"windows": baseline.to_dict()["windows"], "primary_events": baseline.primary["events"]}
        feedback, last = "", None
        for attempt in range(1, MAX_REPAIR_ATTEMPTS + 1):
            self._stage("patch", "running", attempt=attempt)
            record: dict[str, Any] = {"attempt": attempt}
            patch["attempts"].append(record)
            try:
                edits, record["model_reply"] = agent.design_patch(self.model, tools, found, digest, feedback, attempt)
                diff = patching.edits_to_diff(tools.original.files, edits)
                record["diff"] = diff
                record["files_changed"] = tools.call("apply_patch", diff=diff)
            except (patching.Rejected, PolicyDenied) as exc:
                record["status"], record["reason"] = "rejected", str(exc)
                feedback = f"rejected by patch validation: {exc}"
                self._stage("patch", "rejected", attempt=attempt, reason=str(exc))
                continue
            self._stage("patch", "applied", attempt=attempt, files=record["files_changed"])
            self._stage("rerun", "running", attempt=attempt)
            try:
                last = tools.call("run_test")
            except TestRunError as exc:
                record["status"], record["reason"] = "run_failed", str(exc)[:300]
                feedback = f"patched code crashed the locked test: {exc}"
                tools.call("revert_patch")
                continue
            record["status"], record["outcome"] = "executed", last.outcome.value
            record["windows"] = {w["window"]: w["verdict"]["outcome"] for w in last.windows}
            self._stage("rerun", "done", attempt=attempt, outcome=last.outcome.value)
            if last.outcome is Outcome.STOPPED:
                patch.update(applied=True, diff=diff, files_changed=record["files_changed"])
                return last
            feedback = "the locked test still fails after your patch: " + json.dumps({w["window"]: w["verdict"]["reason"] for w in last.windows if w["verdict"]["outcome"] != "STOPPED"})
            tools.call("revert_patch")
        return last

    def _metrics(self, started: float) -> dict[str, Any]:
        t = self.tools
        return {"verification_seconds": round(time.monotonic() - started, 1), "model_calls": self.model.calls, "model_cache_hits": self.model.cache_hits,
                "model_tracing": getattr(self.model, "tracing", False),
                "sandbox_runs": t.sandbox_runs if t else 0, "sandbox_failures": t.sandbox_failures if t else 0}

    def _write(self, evidence: dict[str, Any]) -> None:
        run_dir = self.out_dir / evidence["run_id"]
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True))
        (run_dir / "summary.txt").write_text(evidence["summary"])
        if evidence.get("test"):
            (run_dir / "race_test.py").write_text(evidence["test"]["source"])
        if evidence.get("patch", {}).get("diff"):
            (run_dir / "patch.diff").write_text(evidence["patch"]["diff"])
        evidence["evidence_path"] = str(run_dir / "evidence.json")


def summarize(ev: dict[str, Any]) -> str:
    """Concise, deterministic, fact-only summary derived from the structured evidence."""
    lines = [f"StopProof run {ev.get('run_id')} on {ev.get('repository', {}).get('path', '?')}",
             f"Executed in:  {ev.get('execution', {}).get('label', 'unknown')}"]
    a = ev.get("analysis", {}).get("proposal")
    if a:
        lines += [f"Handoff:      {a['handoff']['file']}:{a['handoff']['symbol']} (line {a['handoff']['line']})",
                  f"STOP control: {a['stop_control']['file']}:{a['stop_control']['symbol']} (line {a['stop_control']['line']})",
                  f"Worker:       {a['worker_entry']['file']}:{a['worker_entry']['symbol']}",
                  f"Sink:         {a['protected_sink']['file']}:{a['protected_sink']['symbol']} (protected)"]
    if ev.get("test"):
        lines.append(f"Locked test:  sha256 {ev['test']['sha256'][:16]}... exercised {len(ev['test']['race_windows'])} race windows")
    for key, title in (("baseline", "Baseline"), ("after_patch", "After patch")):
        run = ev.get(key)
        if run:
            lines.append(f"{title}: {run['outcome']}  ({run['sandbox'].get('backend') or 'sandbox'} op {run['sandbox'].get('operation_id')})")
            for w in run["windows"]:
                lines.append(f"  {w['window']:<24} {w['verdict']['outcome']:<18} {w['verdict']['reason']}")
    p = ev.get("patch", {})
    if p.get("attempted"):
        lines.append(f"Patch: {'applied to ' + ', '.join(p['files_changed']) if p.get('applied') else 'no attempt fixed the failure'}; same locked test re-run: {ev.get('same_test_verified')}")
    if ev.get("error"):
        lines.append(f"ERROR at {ev['error']['stage']}: {ev['error']['message']}")
    v = ev.get("verdict", {})
    lines.append(f"VERDICT: {v.get('outcome')} - {v.get('reason')}")
    return "\n".join(lines) + "\n"
