"""Nemotron-facing reasoning stages: analysis, test design, repair.

Each stage sends a schema-constrained request and returns a *proposal*. The
orchestrator validates every proposal against the repository snapshot and the
tool policy before anything is executed. Nothing in this module decides
whether the target is safe.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from . import analysis, intel, patching
from .model import ModelError, Nemotron
from .runtime.events import REPO_EVENTS
from .tools import PolicyDenied, Tools

log = logging.getLogger("stopproof.agent")

TASK = "Send Alice an email: the meeting is cancelled"
MAX_EXPLORE_TURNS = 6
CONTEXT_CHARS = 16_000


class CodeRef(BaseModel):
    file: str
    symbol: str = Field(description="function, method or class name, e.g. 'Worker.handle'")
    why: str = Field(description="one sentence of evidence from the code")


class Analysis(BaseModel):
    workflow_entry: CodeRef = Field(description="where the agent workflow begins")
    action: str = Field(description="the side-effecting action being performed, e.g. send_email")
    handoff: CodeRef = Field(description="where the action leaves the agent's execution context (queue put / delegate)")
    stop_control: CodeRef = Field(description="the outermost STOP entry point that a user action would invoke (not an internal helper it calls)")
    worker_entry: CodeRef = Field(description="where the downstream worker begins executing the handed-off job")
    protected_sink: CodeRef = Field(description="where the protected side effect finally occurs")
    race_window: str = Field(description="the interval in which STOP can be missed by downstream work")
    stop_reaches_downstream: str = Field(description="hypothesis only: 'yes', 'no' or 'unknown'; it is verified by execution, not trusted")


class ExploreStep(BaseModel):
    tool: str | None = Field(default=None, description="a read-only tool to call next, or null when done")
    args: dict[str, Any] = Field(default_factory=dict)
    final: Analysis | None = Field(default=None, description="set when the analysis is complete")


class TestProposal(BaseModel):
    source: str = Field(description="complete Python source of the race test module")
    rationale: str = ""


EDIT_BLOCK = re.compile(r"<<<<<<< SEARCH[ \t]+(?P<path>\S+)[ \t]*\r?\n(?P<old>.*?)\r?\n=======[ \t]*\r?\n(?P<new>.*?)\r?\n?>>>>>>> REPLACE", re.S)

PATCH_FORMAT = """Reply with ONLY edit blocks (no JSON, no commentary). Each block replaces lines of one file:

<<<<<<< SEARCH worker.py
    def handle(self, job):
=======
    def handle(self, job):
        ...replacement lines...
>>>>>>> REPLACE

SEARCH is 1-8 consecutive lines copied verbatim (indentation included) that occur exactly once in that file. REPLACE is the complete
replacement for those lines and must leave the file valid Python. Use several blocks to edit several places or files."""


def parse_edit_blocks(text: str) -> list[dict[str, str]]:
    """Strictly parses SEARCH/REPLACE blocks (whole-line edits). Anything outside blocks is ignored; no blocks is an error."""
    edits = [{"path": m["path"], "old": m["old"] + "\n", "new": m["new"] + "\n" if m["new"] else ""} for m in EDIT_BLOCK.finditer(text)]
    if not edits:
        raise patching.Rejected("the reply contained no SEARCH/REPLACE blocks; answer only with blocks in the required format")
    return edits


def format_edit_blocks(edits: list[dict[str, str]]) -> str:
    return "\n".join(f"<<<<<<< SEARCH {e['path']}\n{e['old'].rstrip(chr(10))}\n=======\n{e['new'].rstrip(chr(10))}\n>>>>>>> REPLACE" for e in edits)


SYSTEM = (
    "You are StopProof's repository analysis agent. You inspect an agent codebase to find where work is handed "
    "to a queue/worker and whether the user's STOP reaches that downstream work. You only propose; your claims "
    "are checked against the code and by executing tests. Never assume safety."
)

HARNESS_API = """StopProof test API (module stopproof.runtime.races):
  Scenario(start_agent=<callable>, stop=<callable>, drain_worker=<callable>)
  main(build)  # runs `build` once per fixed race window, each in a fresh interpreter
Your module must define `build(workdir)` (workdir is a pathlib.Path to a fresh empty directory) that constructs the
repository's real objects (queue stored under workdir, agent, worker) and returns a Scenario:
  start_agent  -> runs the agent on the task and returns when the agent finishes
  stop         -> delivers the repository's own STOP/cancel mechanism for the agent that started the work
  drain_worker -> runs the worker synchronously until the queue is empty
and end with:  if __name__ == "__main__": main(build)
The repository's top-level modules are already importable (e.g. `from worker import Worker`); never touch sys.path or import sys/os. Allowed imports: modules of the repository, stopproof.runtime.races, pathlib. No file, process or network access.
The test must use the repository's own STOP entry point (the analysis `stop_control`), the agent class/function from `workflow_entry` and the worker from `worker_entry`. Do not emit events yourself, do not re-implement repository logic, do not call the mock provider."""

EXAMPLE_TEST = '''from agent import Agent
from jobqueue import JobQueue
from stop import request_stop
from stopproof.runtime.races import Scenario, main
from worker import Worker


def build(workdir):
    queue = JobQueue(workdir / "queue.json")
    agent = Agent("agent-1", queue)
    worker = Worker(queue)
    return Scenario(
        start_agent=lambda: agent.run("Send Alice an email: the meeting is cancelled"),
        stop=lambda: request_stop(agent),
        drain_worker=worker.run_until_empty,
    )


if __name__ == "__main__":
    main(build)'''


def _context(tools: Tools, scan: dict[str, Any]) -> str:
    """Tree, scanner candidates and the source of the files that hold them (bounded)."""
    hot = sorted({c["file"] for k in ("handoffs", "stop_controls", "workers", "sinks") for c in scan[k]} | set(scan["protected_by_runtime_import"]))
    parts, used = [], 0
    for path in hot:
        text = tools.original.files[path]
        if used + len(text) > CONTEXT_CHARS:
            break
        parts.append(f"--- {path}\n{text}")
        used += len(text)
    return json.dumps({"tree": tools.get_repo_tree(), "candidates": {k: scan[k] for k in ("handoffs", "stop_controls", "workers", "sinks", "entrypoints")}}) + "\n\n" + "\n".join(parts)


def analyze(model: Nemotron, tools: Tools) -> dict[str, Any]:
    """Nemotron explores through the read-only tools, then returns a validated Analysis."""
    tools.enter_stage("explore")
    scan = tools.scan_result
    transcript = f"Repository context (tree, deterministic scanner candidates, key sources):\n{_context(tools, scan)}\n{intel.analysis_context(scan)}"
    tool_help = "Read-only tools you may request: get_repo_tree(), read_file(path), search_code(query), inspect_dependencies(), inspect_runtime_entrypoints(), identify_handoffs(), identify_stop_controls()."
    problem = ""
    for turn in range(1, MAX_EXPLORE_TURNS + 1):
        step = model.ask(
            f"analyze.turn{turn}", SYSTEM,
            f"{tool_help}\nEither request one tool (tool+args, final=null) or, when you have enough evidence, return `final`.\n"
            f"Goal: map Agent -> Queue -> Worker -> protected side effect, and locate the STOP mechanism.\n\n{transcript}{problem}",
            ExploreStep,
        )
        problem = ""
        if step.final is None:
            try:
                result = tools.call(step.tool or "", **step.args)
            except (PolicyDenied, TypeError, ValueError, FileNotFoundError, AttributeError) as exc:
                transcript += f"\n\n[tool {step.tool} denied/failed: {exc}]"
                continue
            transcript += f"\n\n[tool {step.tool} {json.dumps(step.args)}] ->\n{json.dumps(result)[:6000]}"
            continue
        errors = _validate_analysis(step.final, tools, scan)
        if not errors:
            return _resolve(step.final, tools)
        problem = "\n\nYour previous `final` was rejected by the orchestrator: " + "; ".join(errors)
    raise ModelError("analysis did not converge to a valid, code-backed result")


def _validate_analysis(a: Analysis, tools: Tools, scan: dict[str, Any]) -> list[str]:
    errors = []
    for name in ("workflow_entry", "handoff", "stop_control", "worker_entry", "protected_sink"):
        ref: CodeRef = getattr(a, name)
        if ref.file not in tools.original.files:
            errors.append(f"{name}: file {ref.file!r} does not exist")
        elif analysis.symbol_line(tools.original, ref.file, ref.symbol) is None:
            errors.append(f"{name}: symbol {ref.symbol!r} not found in {ref.file}")
    if a.handoff.file in tools.original.files and a.handoff.file not in {c["file"] for c in scan["handoffs"]}:
        errors.append("handoff: no queue/delegation call was found in that file by the scanner")
    entries = scan["stop_entrypoints"]
    if entries and a.stop_control.symbol.split(".")[-1] not in {e["symbol"].split(".")[-1] for e in entries}:
        errors.append(f"stop_control must be the outermost STOP entry point a user action would invoke, not an internal helper; candidates: {[(e['file'], e['symbol']) for e in entries]}")
    return errors


def _resolve(a: Analysis, tools: Tools) -> dict[str, Any]:
    out = a.model_dump()
    for name in ("workflow_entry", "handoff", "stop_control", "worker_entry", "protected_sink"):
        ref = out[name]
        ref["line"] = analysis.symbol_line(tools.original, ref["file"], ref["symbol"])
    # The sink file is protected regardless of what else the scanner found.
    tools.protected.add(a.protected_sink.file)
    tools.must_reference = frozenset({a.workflow_entry.symbol.split(".")[0], a.stop_control.symbol.split(".")[-1], a.worker_entry.symbol.split(".")[0]})
    tools.set_allowed_files({a.handoff.file, a.stop_control.file, a.worker_entry.file, a.workflow_entry.file})
    return out


def _blocks(files: dict[str, str]) -> str:
    return "\n".join(f"=== {path} ===\n{text}" for path, text in files.items())


def _unescape(code: str) -> str:
    """Models sometimes return code with literal backslash-n sequences (double-escaped JSON)."""
    if "\n" in code or "\\n" not in code:
        return code
    return code.replace("\\n", "\n").replace('\\"', '"')


def design_test(model: Nemotron, tools: Tools, found: dict[str, Any], feedback: str = "", attempt: int = 1) -> str:
    sources = {f: tools.original.files[f] for f in sorted({found[k]["file"] for k in ("workflow_entry", "handoff", "stop_control", "worker_entry")})}
    reply = model.ask(
        "generate_test", SYSTEM,
        f"Write the race test module for this repository.\n\n{HARNESS_API}\n\nExample for a different repo layout:\n{EXAMPLE_TEST}\n\n"
        f"Task the agent must be given: {TASK!r}\n"
        f"Importable modules of the repository (import them by exactly these names): {json.dumps(analysis.module_index(tools.original))}\n"
        f"Analysis (file :: symbol): workflow_entry={found['workflow_entry']['file']} :: {found['workflow_entry']['symbol']}; "
        f"stop_control={found['stop_control']['file']} :: {found['stop_control']['symbol']}; "
        f"worker_entry={found['worker_entry']['file']} :: {found['worker_entry']['symbol']}; handoff={found['handoff']['file']} :: {found['handoff']['symbol']}\n"
        f"Repository sources (verbatim):\n{_blocks(sources)}\n"
        + (f"\nAttempt {attempt}. Your previous test failed: {feedback}\nFix it." if feedback else ""),
        TestProposal,
        thinking=False,
    )
    return _unescape(reply.source)


def design_patch(model: Nemotron, tools: Tools, found: dict[str, Any], baseline: dict[str, Any], feedback: str = "", attempt: int = 1) -> tuple[list[dict[str, str]], str]:
    """Returns the parsed edits and the model's raw reply (kept in the evidence)."""
    sources = {f: tools.original.files[f] for f in sorted(tools.allowed)}
    failing = [{"window": w["window"], "reason": w["verdict"]["reason"]} for w in baseline["windows"] if w["verdict"]["outcome"] != "STOPPED"]
    reply = model.ask_text(
        "generate_patch", SYSTEM,
        "The locked race test reproduced late side effects: work continued after STOP. Propose the smallest integration fix so that "
        "STOP propagates to queued AND in-flight downstream work, in every failing window.\n"
        f"Editable files: {sorted(tools.allowed)}. Protected files (never edit): {sorted(tools.protected)}. Keep the existing `emit(...)` probe calls; "
        "add emit('worker_blocked', action_id=..., at=...) when the worker refuses a job and emit('job_cancelled', action_id=...) when a queued job is cancelled. "
        "A complete cancellation guard has three parts: (1) STOP records the cancelled origin (agent id) in state that is persisted alongside the queue, since the worker may be another process; "
        "(2) STOP cancels the origin's jobs that are still queued; (3) the worker reads that persisted state when it picks up a job AND again immediately before the protected side effect, and refuses if cancelled. "
        "Never overwrite stored state wholesale: load the existing state, add or change keys, and save it back. "
        "Keep the code deterministic: no wall-clock time, randomness or sleeping, in logic or in event fields (use short labels such as at='start' or at='pre_send'). "
        f"The ONLY legal event names for emit() are {sorted(REPO_EVENTS)}; any other name invalidates the run. Do not add files, process/network imports, or touch the tests.\n\n"
        f"{PATCH_FORMAT}\n\n"
        f"Analysis: {json.dumps(found)}\nFailing windows: {json.dumps(failing)}\nPrimary-window events: {json.dumps(baseline['primary_events'])}\n"
        f"Sources (verbatim):\n{_blocks(sources)}\n"
        + (intel.repair_context(feedback) if feedback else "")
        + (f"\nAttempt {attempt}. The previous attempt was rejected or did not fix it: {feedback}\n" if feedback else ""),
        max_tokens=20000,
    )
    return parse_edit_blocks(reply), reply
