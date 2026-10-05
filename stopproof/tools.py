"""The controlled coding-agent tool contract.

The model never touches the filesystem, the sandbox or a subprocess. It asks
for a tool by name; `Tools.call` checks the stage policy and call budget,
runs the tool against an in-memory snapshot, and logs the decision. Anything
that executes code goes to the Token Factory Sandbox via the injected runner.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from . import analysis, patching
from .runtime.races import PRIMARY_WINDOW, RESULT_MARKER, WINDOWS
from .sandbox import build_bundle
from .verifier import Outcome, aggregate, verify

log = logging.getLogger("stopproof.tools")

READ_TOOLS = {"get_repo_tree", "read_file", "search_code", "inspect_dependencies", "inspect_runtime_entrypoints", "identify_handoffs", "identify_stop_controls"}
STAGE_TOOLS = {
    "explore": READ_TOOLS,
    "test": {"generate_test", "run_test", "run_workflow", "collect_events", "reset_environment"},
    "repair": {"apply_patch", "revert_patch", "run_test", "collect_events", "reset_environment"},
}
MAX_CALLS = {"explore": 12, "test": 10, "repair": 12}
MAX_READ_CHARS = 20_000


class PolicyDenied(PermissionError):
    pass


class TestRunError(RuntimeError):
    """The sandbox ran, but the test produced no usable trace (a test problem, not a verdict)."""

    __test__ = False


class SandboxRunner(Protocol):
    def execute(self, bundle: bytes, script: str, env: dict[str, str], timeout: int) -> dict[str, Any]: ...


@dataclass
class RunResult:
    label: str
    run_id: str
    outcome: Outcome
    windows: list[dict[str, Any]]
    test_sha256: str
    sandbox: dict[str, Any]

    @property
    def primary(self) -> dict[str, Any]:
        return next(w for w in self.windows if w["window"] == PRIMARY_WINDOW)

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "run_id": self.run_id, "outcome": self.outcome.value, "primary_window": PRIMARY_WINDOW,
                "test_sha256": self.test_sha256, "windows": self.windows, "sandbox": self.sandbox}


RACE_SCRIPT = """set -eu
cd "$ROOT"
python -c "import tarfile; tarfile.open('bundle.tgz').extractall('.', filter='data')"
echo "$LOCK_SHA  locked/race_test.py" | sha256sum -c -
cd repo
PYTHONPATH="$ROOT/harness:$ROOT/repo" PYTHONDONTWRITEBYTECODE=1 python "$ROOT/locked/race_test.py" --run-id "$RUN_ID"
"""
WORKFLOW_SCRIPT = """set -eu
cd "$ROOT"
python -c "import tarfile; tarfile.open('bundle.tgz').extractall('.', filter='data')"
cd repo
PYTHONPATH="$ROOT/harness:$ROOT/repo" PYTHONDONTWRITEBYTECODE=1 python "$ENTRYPOINT"
"""


class Tools:
    def __init__(self, snapshot: analysis.RepoSnapshot, runner: SandboxRunner, run_id: str | None = None) -> None:
        self.original = snapshot
        self.working = dict(snapshot.files)
        self.runner = runner
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
        self.stage = "explore"
        self._calls: dict[str, int] = {}
        self.scan_result = analysis.scan(snapshot)
        self.repo_modules = {p[:-3].split("/")[0] for p in snapshot.files if p.endswith(".py")}
        self.protected: set[str] = set(self.scan_result["protected_by_runtime_import"])
        self.allowed: set[str] = set()
        self.must_reference: frozenset[str] = frozenset()
        self.locked: patching.LockedTest | None = None
        self.patch: str | None = None
        self.baseline: RunResult | None = None
        self.last_run: RunResult | None = None
        self.sandbox_runs = self.sandbox_failures = 0
        self.audit: list[dict[str, Any]] = []

    # -- policy -----------------------------------------------------------------
    def enter_stage(self, stage: str) -> None:
        self.stage, self._calls = stage, {}

    def call(self, tool: str, **args: Any) -> Any:
        used = self._calls[self.stage] = self._calls.get(self.stage, 0) + 1
        reason = None
        if tool not in STAGE_TOOLS[self.stage]:
            reason = f"tool not permitted in stage {self.stage!r}"
        elif used > MAX_CALLS[self.stage]:
            reason = f"call budget of {MAX_CALLS[self.stage]} exhausted for stage {self.stage!r}"
        self.audit.append({"tool": tool, "stage": self.stage, "allowed": reason is None, "reason": reason, "args": {k: (v if len(str(v)) < 80 else f"<{len(str(v))} chars>") for k, v in args.items()}})
        log.info("tool_request run=%s tool=%s stage=%s allowed=%s%s", self.run_id, tool, self.stage, reason is None, f" reason={reason}" if reason else "")
        if reason:
            raise PolicyDenied(f"{tool}: {reason}")
        return getattr(self, tool)(**args)

    def set_allowed_files(self, extra: set[str]) -> None:
        """Patchable files = files holding handoff/stop/worker logic, minus anything protected."""
        scanned = {c["file"] for key in ("handoffs", "stop_controls", "workers") for c in self.scan_result[key]}
        self.allowed = {p for p in (scanned | extra) if p.endswith(".py")} - self.protected

    # -- read-only tools ----------------------------------------------------------
    def get_repo_tree(self) -> list[str]:
        return sorted(self.original.files)

    def read_file(self, path: str) -> str:
        if path not in self.original.files:
            raise FileNotFoundError(f"{path!r} is not a readable file in the repository snapshot")
        return self.original.files[path][:MAX_READ_CHARS]

    def search_code(self, query: str) -> list[dict[str, Any]]:
        if not query or len(query) > 100:
            raise ValueError("query must be 1-100 characters")
        try:
            pattern = re.compile(query)
        except re.error:
            pattern = re.compile(re.escape(query))
        hits = [{"file": p, "line": n, "text": line.strip()[:160]} for p, text in sorted(self.original.files.items()) for n, line in enumerate(text.splitlines(), 1) if pattern.search(line)]
        return hits[:50]

    def inspect_dependencies(self) -> dict[str, Any]:
        return self.scan_result["dependencies"]

    def inspect_runtime_entrypoints(self) -> list[dict[str, Any]]:
        return self.scan_result["entrypoints"]

    def identify_handoffs(self) -> list[dict[str, Any]]:
        return self.scan_result["handoffs"]

    def identify_stop_controls(self) -> list[dict[str, Any]]:
        return self.scan_result["stop_controls"]

    # -- test generation and locking ---------------------------------------------
    def generate_test(self, source: str) -> patching.LockedTest:
        """Validates and locks a proposed race test. Once a baseline has run against it, it can never change."""
        if self.baseline is not None:
            raise PolicyDenied("the verification test is frozen: a baseline has already run against it")
        self.locked = patching.lock_test(source, self.repo_modules, self.must_reference)
        return self.locked

    # -- execution (sandbox only) --------------------------------------------------
    def _execute(self, script: str, env: dict[str, str], timeout: int, test: patching.LockedTest | None, files: dict[str, str]) -> dict[str, Any]:
        started = time.monotonic()
        self.sandbox_runs += 1
        try:
            result = self.runner.execute(build_bundle(files, test), script, env, timeout)
        except Exception:
            self.sandbox_failures += 1
            raise
        result["seconds"] = round(time.monotonic() - started, 2)
        result["script"] = script.strip().splitlines()
        return result

    def run_test(self, test_path: str = patching.LOCKED_TEST_PATH, label: str | None = None) -> RunResult:
        if self.locked is None or test_path != self.locked.path:
            raise PolicyDenied("only the locked verification test may be run")
        if patching.sha256(self.locked.source) != self.locked.sha256:
            raise PolicyDenied("locked test hash mismatch")
        label = label or ("baseline" if self.patch is None else "after_patch")
        run_id = f"{self.run_id}-{label}"
        info = self._execute(RACE_SCRIPT, {"LOCK_SHA": self.locked.sha256, "RUN_ID": run_id}, 120, self.locked, self.working)
        line = next((l for l in reversed(info["stdout"].splitlines()) if l.startswith(RESULT_MARKER)), None)
        if info["exit_code"] != 0 or line is None:
            tail = (info["stderr"] or info["stdout"]).strip()[-800:]
            raise TestRunError(f"race test produced no trace (exit {info['exit_code']}): {tail}")
        try:
            raw = json.loads(line[len(RESULT_MARKER):])["windows"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise TestRunError(f"race test emitted an unreadable result: {exc}") from exc
        windows = []
        by_name = {w.name: w for w in WINDOWS}
        for trace in raw:
            verdict = verify(trace)
            windows.append({"window": trace.get("window"), "description": by_name[trace["window"]].description if trace.get("window") in by_name else "",
                            "error": trace.get("error"), "events": trace.get("events", []), "verdict": verdict.to_dict()})
        seen = {w["window"] for w in windows}
        for missing in (w for w in WINDOWS if w.name not in seen):  # a window with no trace is never safe
            windows.append({"window": missing.name, "description": missing.description, "error": "no trace returned", "events": [],
                            "verdict": verify(None).to_dict()})
        outcome = aggregate([Outcome(w["verdict"]["outcome"]) for w in windows])
        result = RunResult(label, run_id, outcome, windows, self.locked.sha256,
                           {k: info.get(k) for k in ("backend", "operation_id", "image", "exit_code", "seconds", "bundle_sha256", "script", "networking")}
                           | {"stderr_tail": (info["stderr"] or "")[-400:], "test_sha256_verified_in_sandbox": True})
        self.last_run = result
        if self.patch is None:
            self.baseline = result
        log.info("run_test run=%s label=%s outcome=%s", self.run_id, label, outcome.value)
        return result

    def run_workflow(self) -> dict[str, Any]:
        """Runs the repository's own entrypoint once in the sandbox (smoke test of the real workflow)."""
        entry = next((e["file"] for e in self.scan_result["entrypoints"] if e["file"] in self.working), None)
        if entry is None:
            return {"skipped": "no runtime entrypoint found"}
        info = self._execute(WORKFLOW_SCRIPT, {"ENTRYPOINT": entry}, 60, None, self.working)
        return {"entrypoint": entry, "exit_code": info["exit_code"], "stdout": info["stdout"][-400:], "stderr": info["stderr"][-400:],
                "operation_id": info.get("operation_id"), "seconds": info["seconds"]}

    def collect_events(self) -> list[dict[str, Any]]:
        if self.last_run is None:
            raise PolicyDenied("no run has been executed")
        return [{"window": w["window"], "events": w["events"]} for w in self.last_run.windows]

    # -- repair -------------------------------------------------------------------
    def apply_patch(self, diff: str) -> list[str]:
        if self.locked is None or self.baseline is None:
            raise PolicyDenied("patches are only accepted after the test is locked and a baseline has run")
        if self.baseline.outcome is not Outcome.STILL_ABLE_TO_ACT:
            raise PolicyDenied("a repair is only attempted when the baseline reproduced a late effect")
        if not self.allowed:
            raise PolicyDenied("no patchable files were established by the analysis")
        self.working = patching.apply_diff(diff, self.original.files, self.allowed, self.protected)
        self.patch = diff
        return sorted(p for p in self.working if self.working[p] != self.original.files[p])

    def revert_patch(self) -> None:
        self.working, self.patch = dict(self.original.files), None

    def reset_environment(self) -> str:
        """Drops patch, locked test, run state and event history and issues a fresh run id.

        Sandbox instances are disposable, so no filesystem state can carry over.
        """
        self.revert_patch()
        self.last_run = self.baseline = self.locked = None
        self.run_id = f"run-{uuid.uuid4().hex[:8]}"
        return self.run_id
