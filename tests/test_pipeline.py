"""Pipeline, tool policy, patch safety and test locking.

The model is scripted and the sandbox is a local double: these tests exercise StopProof's own
control flow. The real Nemotron / Sandbox integrations are covered by tests/test_integration_live.py.
"""
import json
from pathlib import Path

import pytest
from local_sandbox import LocalSandbox

from stopproof import agent, analysis, patching
from stopproof.orchestrator import Pipeline
from stopproof.tools import PolicyDenied, Tools
from stopproof.verifier import Outcome

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"
REFERENCE = (ROOT / "tests" / "reference_race.py").read_text()


def ref(file, symbol):
    return agent.CodeRef(file=file, symbol=symbol, why="test")


ANALYSIS = agent.Analysis(
    workflow_entry=ref("agent.py", "Agent.run"), action="send_email", handoff=ref("agent.py", "enqueue"),
    stop_control=ref("stop.py", "request_stop"), worker_entry=ref("worker.py", "Worker.handle"),
    protected_sink=ref("provider.py", "send_email"), race_window="between enqueue and worker start", stop_reaches_downstream="unknown",
)


def fixed_edits():
    edits = []
    for name in ("jobqueue.py", "worker.py", "stop.py"):
        edits.append({"path": name, "old": (DEMO / "vulnerable_agent" / name).read_text(), "new": (DEMO / "fixed_agent" / name).read_text()})
    return edits


class ScriptedModel:
    calls = cache_hits = 0

    def __init__(self, patches=None, test=REFERENCE):
        self.patches, self.test, self.patch_calls = patches or [fixed_edits()], test, 0

    def ask(self, stage, system, user, schema, **_):
        if stage.startswith("analyze"):
            return agent.ExploreStep(final=ANALYSIS)
        return agent.TestProposal(source=self.test, rationale="scripted")

    def ask_text(self, stage, system, user, **_):
        edits = self.patches[min(self.patch_calls, len(self.patches) - 1)]
        self.patch_calls += 1
        return agent.format_edit_blocks(edits)


def run_pipeline(repo, model, tmp_path):
    return Pipeline(DEMO / repo, model, LocalSandbox(), tmp_path).run()


def test_vulnerable_repo_full_chain(tmp_path):
    ev = run_pipeline("vulnerable_agent", ScriptedModel(), tmp_path)
    assert ev["baseline"]["outcome"] == "STILL ABLE TO ACT"
    assert ev["after_patch"]["outcome"] == "STOPPED"
    assert ev["verdict"]["outcome"] == "STOPPED"
    assert ev["patch"]["applied"] and ev["patch"]["files_changed"] == ["jobqueue.py", "stop.py", "worker.py"]
    assert ev["same_test_verified"] and ev["baseline"]["test_sha256"] == ev["after_patch"]["test_sha256"] == ev["test"]["sha256"]
    assert (tmp_path / ev["run_id"] / "evidence.json").exists() and (tmp_path / ev["run_id"] / "patch.diff").exists()
    assert "VERDICT: STOPPED" in ev["summary"]


def test_fixed_repo_needs_no_patch(tmp_path):
    ev = run_pipeline("fixed_agent", ScriptedModel(), tmp_path)
    assert ev["verdict"]["outcome"] == "STOPPED" and not ev["patch"]["attempted"] and ev["after_patch"] is None


def test_opaque_repo_cannot_establish_and_is_not_repaired(tmp_path):
    ev = run_pipeline("opaque_agent", ScriptedModel(), tmp_path)
    assert ev["verdict"]["outcome"] == "CANNOT ESTABLISH" and not ev["patch"]["attempted"]


def test_a_patch_that_does_not_fix_the_failure_is_never_reported_as_stopped(tmp_path):
    noop = [{"path": "stop.py", "old": "    agent.cancel()", "new": "    agent.cancel()  # cancel"}]
    ev = run_pipeline("vulnerable_agent", ScriptedModel(patches=[noop]), tmp_path)
    assert ev["verdict"]["outcome"] == "STILL ABLE TO ACT" and not ev["patch"]["applied"]
    assert all(a["outcome"] == "STILL ABLE TO ACT" for a in ev["patch"]["attempts"])


@pytest.mark.parametrize(
    "edit, why",
    [
        ({"path": "provider.py", "old": "    _provider.send(to, body, action_id)", "new": "    pass"}, "protected"),
        ({"path": "probe.py", "old": "        return None", "new": "        return 1"}, "protected"),
        ({"path": "main.py", "old": "    Worker(queue).run_until_empty()", "new": "    pass"}, "allowed list"),
        ({"path": "../evil.py", "old": "x", "new": "y"}, "unknown file"),
        ({"path": "worker.py", "old": "        body = job[\"body\"].strip()", "new": "        import subprocess; body = job[\"body\"].strip()"}, "forbidden construct"),
        ({"path": "worker.py", "old": "    def handle(self, job):", "new": "    def handle(self, job:"}, "does not parse"),
    ],
)
def test_suspicious_patches_are_rejected(tmp_path, edit, why):
    ev = run_pipeline("vulnerable_agent", ScriptedModel(patches=[[edit]]), tmp_path)
    assert not ev["patch"]["applied"] and ev["verdict"]["outcome"] == "STILL ABLE TO ACT"
    assert why in json.dumps(ev["patch"]["attempts"])


def make_tools(repo="vulnerable_agent"):
    tools = Tools(analysis.load_snapshot(DEMO / repo), LocalSandbox())
    tools.set_allowed_files({"agent.py"})
    return tools


@pytest.mark.parametrize(
    "source",
    ["import os\n" + REFERENCE, REFERENCE.replace("main(build)", "pass"), REFERENCE.replace("Scenario(", "dict("), "def build(:\n", REFERENCE + "\nopen('x')\n",
     REFERENCE + "\nx = build.__globals__\n", "import socket\n" + REFERENCE],
)
def test_generated_test_must_pass_static_validation(source):
    with pytest.raises(patching.Rejected):
        make_tools().generate_test(source)


def test_test_is_locked_once_baseline_has_run():
    tools = make_tools()
    tools.enter_stage("test")
    tools.call("generate_test", source=REFERENCE)
    assert tools.call("run_test").outcome is Outcome.STILL_ABLE_TO_ACT
    with pytest.raises(PolicyDenied):
        tools.call("generate_test", source=REFERENCE.replace("cancelled", "ok"))
    object.__setattr__(tools.locked, "source", REFERENCE + "# tampered")
    with pytest.raises(PolicyDenied):
        tools.call("run_test")


def test_tool_policy_by_stage_and_budget():
    tools = make_tools()
    with pytest.raises(PolicyDenied):
        tools.call("apply_patch", diff="")
    with pytest.raises(PolicyDenied):
        tools.call("run_test")
    with pytest.raises(PolicyDenied, match="budget"):
        for _ in range(20):
            tools.call("get_repo_tree")
    assert [a["allowed"] for a in tools.audit][-1] is False


def test_patch_requires_reproduced_failure_and_baseline():
    tools = make_tools()
    tools.enter_stage("repair")
    with pytest.raises(PolicyDenied):
        tools.call("apply_patch", diff="--- a/x\n+++ b/x\n")


def test_read_tools_are_confined_to_the_snapshot():
    tools = make_tools()
    assert "agent.py" in tools.call("get_repo_tree")
    with pytest.raises(FileNotFoundError):
        tools.call("read_file", path="../../.env")
    assert any(h["file"] == "agent.py" for h in tools.call("search_code", query="enqueue"))


def test_reset_environment_clears_run_state():
    tools = make_tools()
    tools.enter_stage("test")
    tools.call("generate_test", source=REFERENCE)
    tools.call("run_test")
    old = tools.run_id
    tools.call("reset_environment")
    assert tools.run_id != old and tools.locked is None and tools.last_run is None and tools.working == tools.original.files


def test_snapshot_skips_secrets(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / ".env").write_text("KEY=secret\n")
    (tmp_path / "creds.pem").write_text("secret")
    assert set(analysis.load_snapshot(tmp_path).files) == {"a.py"}


def test_unparseable_repo_fails_clearly(tmp_path):
    (tmp_path / "a.py").write_text("def broken(:\n")
    with pytest.raises(ValueError):
        analysis.scan(analysis.load_snapshot(tmp_path))


def test_pipeline_failure_is_cannot_establish_not_stopped(tmp_path):
    class Down(ScriptedModel):
        def ask(self, *a, **k):
            raise agent.ModelError("model unavailable")

    ev = run_pipeline("vulnerable_agent", Down(), tmp_path)
    assert ev["verdict"]["outcome"] == "CANNOT ESTABLISH" and ev["error"]["type"] == "ModelError"


def test_scanner_finds_the_mvp_boundary():
    scan = analysis.scan(analysis.load_snapshot(DEMO / "vulnerable_agent"))
    assert any(h["file"] == "agent.py" and h["call"] == "enqueue" for h in scan["handoffs"])
    assert any(c["file"] == "stop.py" for c in scan["stop_controls"])
    assert {"probe.py", "provider.py"} <= set(scan["protected_by_runtime_import"])


VACUOUS = REFERENCE.replace("request_stop(agent)", "agent.cancel()").replace("worker.run_until_empty", "None").replace("from stop import request_stop\n", "").replace("from worker import Worker\n", "").replace("    worker = Worker(queue)\n", "")


def test_vacuous_test_that_skips_the_real_stop_and_worker_is_rejected():
    tools = make_tools()
    tools.must_reference = frozenset({"Agent", "request_stop", "Worker"})
    with pytest.raises(patching.Rejected, match="never uses"):
        tools.generate_test(VACUOUS)
    assert tools.generate_test(REFERENCE).sha256 == patching.sha256(REFERENCE)


def test_scanner_reports_outermost_stop_entrypoint():
    scan = analysis.scan(analysis.load_snapshot(DEMO / "vulnerable_agent"))
    assert [e["symbol"] for e in scan["stop_entrypoints"]] == ["request_stop"]
