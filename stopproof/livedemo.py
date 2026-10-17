"""Live demo runner: the same race windows on the host against a REAL sink.

This is not the verification path. Verification runs the repo inside an
execution backend with the mock provider. Here, the demo repos' provider
delivers a real outbound notification (STOPPROOF_SINK_URL, e.g. an ntfy.sh
topic), so a STILL ABLE TO ACT result means a real notification actually
escaped after STOP — the show-don't-tell layer for the same traces.

Each window runs in a child process exactly like the sandbox race driver;
the deterministic verifier still decides the outcome of every window.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .runtime.races import TRACE_MARKER, WINDOWS, run_window
from .verifier import Outcome, aggregate, verify

TASK = "Send Alice an email: the meeting is cancelled"
CHILD_TIMEOUT_S = 45
REPO_ROOT = Path(__file__).resolve().parent.parent


def _build(workdir: Path):
    """The scenario builder generated tests use, bound to this demo's module layout."""
    from agent import Agent
    from jobqueue import JobQueue
    from stop import request_stop
    from worker import Worker

    from .runtime.races import Scenario

    queue = JobQueue(workdir / "queue.json")
    agent = Agent("agent-1", queue)
    return Scenario(
        start_agent=lambda: agent.run(TASK),
        stop=lambda: request_stop(agent),
        drain_worker=Worker(queue).run_until_empty,
    )


def _child(repo: Path, window: str, run_id: str, sink_url: str) -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{REPO_ROOT}{os.pathsep}{repo}"
    env["STOPPROOF_SINK_URL"] = sink_url
    env["STOPPROOF_LIVE_LABEL"] = window
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "stopproof.livedemo", "--repo", str(repo), "--window", window, "--run-id", run_id],
            capture_output=True, text=True, timeout=CHILD_TIMEOUT_S, env=env, cwd=REPO_ROOT, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"window": window, "error": "timeout", "run_id": run_id, "events": []}
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(TRACE_MARKER):
            return json.loads(line[len(TRACE_MARKER):])
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
    return {"window": window, "error": f"child exited {proc.returncode} without a trace: {' | '.join(tail)}", "run_id": run_id, "events": []}


def run_selfcheck(repo: Path) -> list[dict[str, Any]]:
    """The repo's own conventional checks (selfcheck.py) — what a normal test
    suite asserts about cancel(). They all pass even when the agent can still
    act: that gap between 'tests green' and 'provably stopped' is the point."""
    repo = repo.resolve()
    script = repo / "selfcheck.py"
    if not script.exists():
        return []
    try:
        proc = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, timeout=CHILD_TIMEOUT_S,
            env={**os.environ, "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{repo}"}, cwd=repo, check=False,
        )
    except subprocess.TimeoutExpired:
        return [{"name": "selfcheck.py", "passed": False, "error": "timeout"}]
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("SELFCHECK "):
            return json.loads(line[len("SELFCHECK "):]).get("checks", [])
    return [{"name": "selfcheck.py", "passed": False, "error": f"no result (exit {proc.returncode})"}]


def run_live(repo: Path, sink_url: str, run_id: str | None = None) -> dict[str, Any]:
    """Every window, on the host, with the real sink. The verifier still decides."""
    repo = repo.resolve()
    run_id = run_id or f"live-{uuid.uuid4().hex[:8]}"
    windows = []
    for w in WINDOWS:
        trace = _child(repo, w.name, f"{run_id}:{w.name}", sink_url)
        windows.append({"window": w.name, "description": w.description, "error": trace.get("error"),
                        "events": trace.get("events", []), "verdict": verify(trace).to_dict()})
    outcome = aggregate([Outcome(w["verdict"]["outcome"]) for w in windows])
    sink_schemes = ", ".join(sorted({urlparse(u.strip()).scheme for u in sink_url.split(",") if u.strip()}))
    return {"run_id": run_id, "repo": repo.name, "sink": sink_schemes, "mode": "live-host (not sandboxed; real side-effect delivery)",
            "outcome": outcome.value, "windows": windows}


def main() -> None:
    parser = argparse.ArgumentParser(prog="stopproof.livedemo")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--window", required=True)
    parser.add_argument("--run-id", default="live")
    args = parser.parse_args()
    sys.path.insert(0, args.repo)
    trace = run_window(_build, next(w for w in WINDOWS if w.name == args.window), args.run_id)
    print(TRACE_MARKER + json.dumps(trace, sort_keys=True))


if __name__ == "__main__":
    main()
