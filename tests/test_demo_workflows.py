"""Integration + end-to-end: queue -> worker -> mock provider, run through the race driver."""
import json

import pytest
from conftest import ROOT, run_race_locally

from stopproof.runtime.races import PRIMARY_WINDOW, WINDOWS
from stopproof.verifier import Outcome, verify

S, ST, C = Outcome.STILL_ABLE_TO_ACT, Outcome.STOPPED, Outcome.CANNOT_ESTABLISH
EXPECTED = {
    "vulnerable_agent": dict(zip((w.name for w in WINDOWS), (S, ST, S, S, S, ST))),
    "fixed_agent": {w.name: ST for w in WINDOWS},
}


def outcomes(result):
    return {w["window"]: verify(w).outcome for w in result["windows"]}


@pytest.mark.parametrize("repo", EXPECTED)
def test_every_window_matches_known_outcome(demo_results, repo):
    assert outcomes(demo_results[repo]) == EXPECTED[repo]


def test_vulnerable_primary_trace_matches_spec_ordering(demo_results):
    window = demo_results["vulnerable_agent"]["windows"][0]
    assert window["window"] == PRIMARY_WINDOW
    order = [e["event"] for e in window["events"]]
    for earlier, later in [("job_enqueued", "stop_requested"), ("stop_requested", "agent_cancelled"),
                           ("agent_cancelled", "worker_started"), ("worker_started", "provider_send")]:
        assert order.index(earlier) < order.index(later)


def test_fixed_primary_blocks_worker_and_never_sends(demo_results):
    order = [e["event"] for e in demo_results["fixed_agent"]["windows"][0]["events"]]
    assert "job_cancelled" in order and "provider_send" not in order


def test_opaque_worker_yields_cannot_establish_where_it_matters(demo_results):
    got = outcomes(demo_results["opaque_agent"])
    assert got[PRIMARY_WINDOW] is C
    assert Outcome.STOPPED not in {got[w] for w in ("stop_after_queue", "stop_worker_starting", "stop_worker_processing")}


def test_runs_are_reproducible(demo_results):
    again = run_race_locally(ROOT / "demo" / "vulnerable_agent")
    assert json.dumps(again, sort_keys=True) == json.dumps(demo_results["vulnerable_agent"], sort_keys=True)


def test_cancellation_guard_state_is_isolated_between_runs(tmp_path):
    """A reset (fresh queue file) carries no cancellation or jobs from a previous run."""
    import sys
    sys.path.insert(0, str(ROOT / "demo" / "fixed_agent"))
    try:
        from jobqueue import JobQueue
        first = JobQueue(tmp_path / "a.json")
        first.cancel_origin("agent-1")
        second = JobQueue(tmp_path / "b.json")
        assert not second.is_cancelled({"origin": "agent-1"}) and second.pending_count() == 0
    finally:
        sys.path.remove(str(ROOT / "demo" / "fixed_agent"))
        for m in ("jobqueue", "probe"):
            sys.modules.pop(m, None)
