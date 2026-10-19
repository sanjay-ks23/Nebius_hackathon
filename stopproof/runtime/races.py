"""Race-window driver used by every generated test.

A test only describes how to build the target's agent / STOP / worker
(a Scenario). This module owns the schedule, so the interleavings are fixed
by StopProof and cannot be weakened by a generated test or a repair patch.

Each window runs in its own interpreter (fresh module state, fresh queue,
fresh provider) on a single thread, and STOP is injected synchronously at a
named event, so every run of a window yields the identical trace.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import events

TRACE_MARKER = "STOPPROOF_TRACE "
RESULT_MARKER = "STOPPROOF_RESULT "
CHILD_TIMEOUT_S = 30


@dataclass(frozen=True)
class RaceWindow:
    name: str
    description: str
    # STOP is delivered synchronously right after this event is recorded.
    # None means the sequential schedule: agent finishes, STOP, then worker.
    stop_after: str | None


WINDOWS = (
    RaceWindow("queue_stop_worker", "queue first, STOP second, worker third", None),
    RaceWindow("stop_before_queue", "STOP after the action is created, before it is queued", "action_created"),
    RaceWindow("stop_after_queue", "STOP immediately after the job is queued", "job_enqueued"),
    RaceWindow("stop_worker_starting", "STOP while the worker is starting the job", "worker_started"),
    RaceWindow("stop_worker_processing", "STOP while the worker is processing, before the send", "worker_processing"),
    RaceWindow("stop_after_commit", "STOP after the action is already committed", "provider_send"),
)
PRIMARY_WINDOW = WINDOWS[0].name


@dataclass
class Scenario:
    start_agent: Callable[[], object]
    stop: Callable[[], object]
    drain_worker: Callable[[], object]


def get_window(name: str) -> RaceWindow:
    for window in WINDOWS:
        if window.name == name:
            return window
    raise ValueError(f"unknown race window {name!r}")


def run_window(build: Callable[[Path], Scenario], window: RaceWindow, run_id: str) -> dict:
    rec = events.start_run(run_id)
    error = None
    with tempfile.TemporaryDirectory(prefix="stopproof-") as workdir:
        scenario = build(Path(workdir))
        stop_delivered = False

        def deliver_stop() -> None:
            nonlocal stop_delivered
            if stop_delivered:
                return
            stop_delivered = True
            events.emit("stop_requested", window=window.name)
            scenario.stop()

        events.emit("run_started", window=window.name)
        if window.stop_after:
            rec.after(window.stop_after, deliver_stop)
        try:
            scenario.start_agent()
            events.emit("agent_returned")
            if window.stop_after is None:
                deliver_stop()
            scenario.drain_worker()
            events.emit("worker_drain_finished")
        except Exception as exc:  # noqa: BLE001 - the trace stays incomplete -> verifier cannot establish
            error = f"{type(exc).__name__}: {exc}"
        events.emit("run_finished", **({"error": error} if error else {}))
    return {"window": window.name, "error": error, **rec.to_dict()}


def _run_child(window: RaceWindow, run_id: str) -> dict:
    proc = subprocess.run(
        [sys.executable, sys.argv[0], "--window", window.name, "--run-id", run_id],
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT_S,
        check=False,
    )
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(TRACE_MARKER):
            return json.loads(line[len(TRACE_MARKER):])
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
    return {
        "window": window.name,
        "error": f"child exited {proc.returncode} without a trace: {' | '.join(tail)}",
        "run_id": run_id,
        "events": [],
    }


def main(build: Callable[[Path], Scenario]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window")
    parser.add_argument("--run-id", default="local")
    args = parser.parse_args()
    if args.window:
        trace = run_window(build, get_window(args.window), args.run_id)
        print(TRACE_MARKER + json.dumps(trace, sort_keys=True))
        return
    results = []
    for window in WINDOWS:
        try:
            results.append(_run_child(window, f"{args.run_id}:{window.name}"))
        except subprocess.TimeoutExpired:
            results.append({"window": window.name, "error": "timeout", "run_id": args.run_id, "events": []})
    print(RESULT_MARKER + json.dumps({"schema": 1, "windows": results}, sort_keys=True))
