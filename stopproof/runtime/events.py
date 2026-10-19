"""Deterministic event model.

Timestamps are logical: every event advances a counter by CLOCK_STEP, so a
trace is byte-for-byte reproducible and ordering never depends on wall time.
This module is shipped into the sandbox, so it must stay stdlib-only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

CLOCK_START = 1000
CLOCK_STEP = 100

# Trust domains: who is allowed to emit which event.
HARNESS_EVENTS = frozenset(
    {"run_started", "stop_requested", "agent_returned", "worker_drain_finished", "run_finished"}
)
SINK_EVENTS = frozenset({"provider_send"})
REPO_EVENTS = frozenset(
    {
        "agent_started",
        "action_created",
        "job_enqueued",
        "agent_cancelled",
        "worker_started",
        "worker_processing",
        "worker_blocked",
        "job_cancelled",
    }
)
KNOWN_EVENTS = HARNESS_EVENTS | SINK_EVENTS | REPO_EVENTS


@dataclass(frozen=True)
class Event:
    event: str
    ts: int
    action_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"event": self.event, "ts": self.ts}
        if self.action_id is not None:
            out["action_id"] = self.action_id
        if self.data:
            out["data"] = self.data
        return out

    @staticmethod
    def from_dict(raw: Any) -> Event:
        if not isinstance(raw, dict):
            raise ValueError(f"event must be an object, got {type(raw).__name__}")  # noqa: TRY004
        name, ts = raw.get("event"), raw.get("ts")
        if name not in KNOWN_EVENTS:
            raise ValueError(f"unknown event {name!r}")
        if not isinstance(ts, int) or isinstance(ts, bool):
            raise ValueError(f"event {name!r} has non-integer ts")  # noqa: TRY004
        action_id, data = raw.get("action_id"), raw.get("data", {})
        if action_id is not None and not isinstance(action_id, str):
            raise ValueError(f"event {name!r} has non-string action_id")
        if not isinstance(data, dict):
            raise ValueError(f"event {name!r} has non-object data")  # noqa: TRY004
        return Event(name, ts, action_id, data)


def parse_trace(raw: Any) -> tuple[str, list[Event]]:
    """Strictly parse {"run_id": ..., "events": [...]}; raises ValueError."""
    if not isinstance(raw, dict) or not isinstance(raw.get("run_id"), str):
        raise ValueError("trace needs a string run_id")  # noqa: TRY004
    events = raw.get("events")
    if not isinstance(events, list):
        raise ValueError("trace needs an events list")  # noqa: TRY004
    return raw["run_id"], [Event.from_dict(e) for e in events]


class Recorder:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.events: list[Event] = []
        self._hooks: dict[str, Callable[[], None]] = {}

    def emit(self, event: str, action_id: str | None = None, **data: Any) -> Event:
        if event not in KNOWN_EVENTS:
            raise ValueError(f"unknown event {event!r}")
        ev = Event(event, CLOCK_START + CLOCK_STEP * len(self.events), action_id, data)
        self.events.append(ev)
        # One-shot hooks run synchronously right after the event is recorded.
        # This is how race windows are pinned: STOP is injected at an exact
        # program point instead of relying on sleeps.
        hook = self._hooks.pop(event, None)
        if hook:
            hook()
        return ev

    def after(self, event: str, hook: Callable[[], None]) -> None:
        self._hooks[event] = hook

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "events": [e.to_dict() for e in self.events]}


_current = Recorder("unstarted")


def start_run(run_id: str) -> Recorder:
    global _current
    _current = Recorder(run_id)
    return _current


def emit(event: str, action_id: str | None = None, **data: Any) -> Event:
    return _current.emit(event, action_id, **data)
