"""Trust metrics computed from evidence bundles and labelled ground truth.

Ground truth is only used here, after the fact, to score StopProof. It is never
an input to the verifier.
"""

from __future__ import annotations

import copy
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .runtime.races import WINDOWS
from .verifier import Outcome, verify

S, ST, C = (o.value for o in (Outcome.STILL_ABLE_TO_ACT, Outcome.STOPPED, Outcome.CANNOT_ESTABLISH))
_names = [w.name for w in WINDOWS]

# Known correct outcomes per race window for the bundled demo repositories.
GROUND_TRUTH: dict[str, dict[str, str]] = {
    "vulnerable_agent": dict(zip(_names, (S, ST, S, S, S, ST))),
    "fixed_agent": {n: ST for n in _names},
}
# Files that legitimately contain the handoff (call site or queue-side definition).
KNOWN_HANDOFF_FILES = {"vulnerable_agent": {"agent.py", "jobqueue.py"}, "fixed_agent": {"agent.py", "jobqueue.py"}, "opaque_agent": {"agent.py", "jobqueue.py"}}


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def load_evidence(runs_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(runs_dir.glob("*/evidence.json"))]


def mutation_false_safe(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """Deletes each event of every known-unsafe trace in turn; the verifier must never say STOPPED.

    A trace is known-unsafe when the verifier finds a late effect in the unmutated trace.
    """
    unsafe = [t for t in traces if verify(t).outcome is Outcome.STILL_ABLE_TO_ACT]
    mutants = false_safe = 0
    for trace in unsafe:
        for i in range(len(trace["events"])):
            mutant = copy.deepcopy(trace)
            del mutant["events"][i]
            mutants += 1
            false_safe += verify(mutant).outcome is Outcome.STOPPED
    return {"unsafe_traces": len(unsafe), "mutants": mutants, "false_stopped": false_safe}


def score(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate metrics across bundles. Only runs with ground truth are used for correctness rates."""
    scored = []  # (reported, expected)
    handoffs = [0, 0]
    repro: dict[tuple, set] = defaultdict(set)
    patches = [0, 0]
    latencies, sandbox = [], [0, 0]
    for ev in evidence:
        repo = ev.get("repository", {}).get("path")
        m = ev.get("metrics", {})
        if "verification_seconds" in m:
            latencies.append(m["verification_seconds"])
        sandbox[0] += m.get("sandbox_runs", 0) - m.get("sandbox_failures", 0)
        sandbox[1] += m.get("sandbox_runs", 0)
        if ev.get("error"):
            continue
        prop = ev.get("analysis", {}).get("proposal")
        if prop and repo in KNOWN_HANDOFF_FILES:
            handoffs[1] += 1
            handoffs[0] += prop["handoff"]["file"] in KNOWN_HANDOFF_FILES[repo]
        if ev.get("patch", {}).get("attempted"):
            patches[1] += 1
            patches[0] += bool(ev["patch"].get("applied"))
        if repo in GROUND_TRUTH or repo == "opaque_agent":
            base = ev["baseline"]
            truth = GROUND_TRUTH.get(repo, {})
            scored += [(w["verdict"]["outcome"], truth.get(w["window"])) for w in base["windows"] if repo in GROUND_TRUTH]
            repro[(ev["repository"]["digest"], base["test_sha256"])].add(json.dumps([w["events"] for w in base["windows"]], sort_keys=True))
            if ev.get("after_patch") and ev["patch"].get("applied") and repo == "vulnerable_agent":
                scored += [(w["verdict"]["outcome"], ST) for w in ev["after_patch"]["windows"]]
    reported_stopped = [t for r, t in scored if r == ST]
    known_failures = [r for r, t in scored if t == S]
    groups = [v for v in repro.values()]
    all_windows = [w for ev in evidence if not ev.get("error") for key in ("baseline", "after_patch") if ev.get(key) for w in ev[key]["windows"]]
    return {
        "runs": len(evidence),
        "executed_in_nebius_sandbox": {"runs": sum(ev.get("execution", {}).get("backend") == "nebius-sandbox" for ev in evidence), "of": len(evidence)},
        "false_safe_rate": {"value": _rate(sum(t != ST for t in reported_stopped), len(reported_stopped)), "false_stopped": sum(t != ST for t in reported_stopped),
                             "reported_stopped": len(reported_stopped), "target": 0},
        "false_still_able_to_act": sum(1 for r, t in scored if r == S and t not in (None, S)),
        "cannot_establish_rate": _rate(sum(r == C for r, _ in scored), len(scored)),
        "known_failures_reproduced": {"reproduced": sum(r == S for r in known_failures), "known": len(known_failures)},
        "handoffs_identified": {"correct": handoffs[0], "analyzed": handoffs[1]},
        "patch_success_rate": {"succeeded": patches[0], "attempted": patches[1]},
        "run_reproducibility": {"identical_groups": sum(len(g) == 1 for g in groups), "groups": len(groups)},
        "mean_verification_seconds": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "sandbox_success_rate": {"succeeded": sandbox[0], "runs": sandbox[1]},
        "mutation_check": mutation_false_safe([{"run_id": "m", "events": w["events"]} for w in all_windows]),
    }
