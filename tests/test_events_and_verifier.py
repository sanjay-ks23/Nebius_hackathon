import copy

import pytest

from stopproof.runtime import events
from stopproof.runtime.races import WINDOWS, PRIMARY_WINDOW
from stopproof.verifier import Outcome, verify


def trace(*items):
    """items: (event, action_id | None) in order; ts follows the logical clock."""
    return {
        "run_id": "t",
        "events": [
            {"event": name, "ts": 1000 + 100 * i, **({"action_id": aid} if aid else {})}
            for i, (name, aid) in enumerate(items)
        ],
    }


VULNERABLE = [
    ("run_started", None), ("agent_started", None), ("action_created", "email-1"), ("job_enqueued", "email-1"),
    ("agent_returned", None), ("stop_requested", None), ("agent_cancelled", None), ("worker_started", "email-1"),
    ("worker_processing", "email-1"), ("provider_send", "email-1"), ("worker_drain_finished", None), ("run_finished", None),
]
BLOCKED = [e for e in VULNERABLE if e[0] != "provider_send"]
BLOCKED.insert(BLOCKED.index(("worker_drain_finished", None)), ("worker_blocked", "email-1"))


def test_recorder_uses_logical_clock_and_is_reproducible():
    def record():
        rec = events.start_run("r")
        events.emit("agent_started")
        events.emit("action_created", action_id="email-1")
        return rec.to_dict()

    first, second = record(), record()
    assert first == second
    assert [e["ts"] for e in first["events"]] == [1000, 1100]


def test_recorder_hook_fires_once_after_its_event():
    rec = events.start_run("r")
    seen = []
    rec.after("job_enqueued", lambda: seen.append(len(rec.events)))
    events.emit("job_enqueued", action_id="a")
    events.emit("job_enqueued", action_id="b")
    assert seen == [1]


def test_unknown_event_rejected():
    with pytest.raises(ValueError):
        events.start_run("r").emit("made_up")
    with pytest.raises(ValueError):
        events.Event.from_dict({"event": "made_up", "ts": 1})


def test_late_send_is_still_able_to_act():
    v = verify(trace(*VULNERABLE))
    assert v.outcome is Outcome.STILL_ABLE_TO_ACT
    assert v.stop_ts == 1500 and v.late_effects[0]["ts"] == 1900


def test_blocked_worker_is_stopped():
    v = verify(trace(*BLOCKED))
    assert v.outcome is Outcome.STOPPED
    assert all(c.passed for c in v.checks)


def test_send_before_stop_is_not_a_late_effect():
    items = [("run_started", None), ("action_created", "email-1"), ("job_enqueued", "email-1"), ("worker_started", "email-1"),
             ("provider_send", "email-1"), ("stop_requested", None), ("agent_returned", None),
             ("worker_drain_finished", None), ("run_finished", None)]
    assert verify(trace(*items)).outcome is Outcome.STOPPED


def test_verifier_is_deterministic():
    t = trace(*BLOCKED)
    assert verify(copy.deepcopy(t)).to_dict() == verify(copy.deepcopy(t)).to_dict()


def without(items, name):
    return [i for i in items if i[0] != name]


@pytest.mark.parametrize(
    "mutated",
    [
        without(BLOCKED, "stop_requested"),          # missing stop boundary
        without(BLOCKED, "worker_blocked"),          # queued job has no observed disposition
        without(BLOCKED, "worker_drain_finished"),   # worker never observed to finish
        without(BLOCKED, "agent_returned"),          # agent never observed to finish
        without(BLOCKED, "run_finished"),            # incomplete trace
        without(BLOCKED, "action_created"),          # ambiguous identity
        [("run_started", None), ("stop_requested", None), ("agent_returned", None),
         ("worker_drain_finished", None), ("run_finished", None)],  # nothing was tested
    ],
)
def test_missing_evidence_is_never_stopped(mutated):
    assert verify(trace(*mutated)).outcome is Outcome.CANNOT_ESTABLISH


def test_uncorrelated_send_before_stop_cannot_be_established():
    items = [("run_started", None), ("action_created", "email-1"), ("job_enqueued", "email-1"), ("provider_send", None),
             ("stop_requested", None), ("agent_returned", None), ("worker_drain_finished", None), ("run_finished", None)]
    assert verify(trace(*items)).outcome is Outcome.CANNOT_ESTABLISH


def test_late_send_wins_even_when_trace_is_otherwise_incomplete():
    assert verify(trace(*without(VULNERABLE, "run_finished"))).outcome is Outcome.STILL_ABLE_TO_ACT


def test_run_error_is_never_stopped():
    t = trace(*BLOCKED)
    t["events"][-1]["data"] = {"error": "RuntimeError: boom"}
    assert verify(t).outcome is Outcome.CANNOT_ESTABLISH


@pytest.mark.parametrize("bad", [None, [], {"run_id": 1, "events": []}, {"run_id": "x", "events": [{"event": "nope", "ts": 1}]}])
def test_malformed_input_cannot_establish(bad):
    assert verify(bad).outcome is Outcome.CANNOT_ESTABLISH


def test_out_of_order_timestamps_cannot_establish():
    t = trace(*BLOCKED)
    t["events"][3]["ts"] = 0
    assert verify(t).outcome is Outcome.CANNOT_ESTABLISH


def test_race_windows_cover_the_specified_orderings():
    assert WINDOWS[0].name == PRIMARY_WINDOW and WINDOWS[0].stop_after is None
    assert [w.stop_after for w in WINDOWS[1:]] == ["action_created", "job_enqueued", "worker_started", "worker_processing", "provider_send"]
