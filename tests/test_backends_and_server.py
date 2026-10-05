"""Execution backends, backend selection, the demo API and independent re-verification."""
import copy
import json
import time
from argparse import Namespace
from pathlib import Path

import pytest
from local_sandbox import LocalSandbox
from starlette.testclient import TestClient
from test_pipeline import REFERENCE, ScriptedModel

from stopproof import cli, server
from stopproof.orchestrator import Pipeline
from stopproof.sandbox import BACKENDS, BubblewrapSandbox, SandboxUnavailable, backend_status, build_bundle, make_sandbox
from stopproof.tools import RACE_SCRIPT
from stopproof.verifier import reverify_bundle

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"


@pytest.fixture(scope="module")
def local_box():
    try:
        return make_sandbox("local")
    except SandboxUnavailable as exc:
        pytest.skip(f"local bubblewrap sandbox unavailable: {exc}")


# ---- bubblewrap command construction (no bwrap needed) -------------------------------------

def test_bubblewrap_command_is_a_closed_box(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "super-secret-value")
    cmd = BubblewrapSandbox()._command(7, "echo hi", {"RUN_ID": "r1"})
    joined = " ".join(cmd)
    assert "super-secret-value" not in joined, "host environment must never reach the sandbox"
    assert {"--unshare-all", "--die-with-parent", "--new-session", "--clearenv"} <= set(cmd)
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert cmd[cmd.index("--uid") + 1] == "65534"
    assert "--share-net" not in cmd and "--bind" not in cmd and "--bind-try" not in cmd, "no writable or shared host paths"
    ro_binds = [(cmd[i + 1], cmd[i + 2]) for i, a in enumerate(cmd) if a == "--ro-bind"]
    assert ro_binds == [("/usr", "/usr")], "only /usr is visible, read-only"
    assert cmd.index("--clearenv") < cmd.index("--setenv")
    assert "RUN_ID" in cmd and "r1" in cmd and cmd[-3:-1] == ["bash", "-c"]


# ---- bubblewrap for real ------------------------------------------------------------------

def test_local_sandbox_executes_a_bundle(local_box):
    result = local_box.check()
    assert result["backend"] == "local-bubblewrap" and result["exit_code"] == 0 and "hi" in result["stdout"]


def test_local_sandbox_isolation(local_box, monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "super-secret-value")
    probe = """python - <<'PY'
import os, socket
def blocked(fn):
    try:
        fn(); return False
    except OSError:
        return True
print("net_blocked", blocked(lambda: socket.create_connection(("1.1.1.1", 53), timeout=2)))
print("usr_readonly", blocked(lambda: open("/usr/x", "w")))
print("home_absent", not os.path.exists("/home"), "mnt_absent", not os.path.exists("/mnt"), "etc_absent", not os.path.exists("/etc"))
print("secrets_in_env", any("KEY" in k or "TOKEN" in k or "NEBIUS" in k for k in os.environ))
print("scratch_writable", open("/work/ok", "w").write("x") == 1)
print("uid", os.getuid())
PY"""
    out = local_box.execute(b"", probe, {}, 30)
    assert out["exit_code"] == 0, out["stderr"]
    lines = dict(line.split(" ", 1) for line in out["stdout"].strip().splitlines())
    assert lines["net_blocked"] == "True" and lines["usr_readonly"] == "True"
    assert lines["home_absent"] == "True mnt_absent True etc_absent True"
    assert lines["secrets_in_env"] == "False" and lines["scratch_writable"] == "True" and lines["uid"] == "65534"


def test_local_sandbox_enforces_the_timeout(local_box):
    out = local_box.execute(b"", "sleep 20", {}, 1)
    assert out["exit_code"] == 124 and "timed out" in out["stderr"]


def test_locked_test_hash_is_enforced_inside_the_sandbox(local_box):
    from stopproof import analysis, patching

    snap = analysis.load_snapshot(DEMO / "vulnerable_agent")
    test = patching.lock_test(REFERENCE, {"agent", "jobqueue", "stop", "worker"})
    bundle = build_bundle(snap.files, test)
    good = local_box.execute(bundle, RACE_SCRIPT, {"LOCK_SHA": test.sha256, "RUN_ID": "t"}, 60)
    bad = local_box.execute(bundle, RACE_SCRIPT, {"LOCK_SHA": "0" * 64, "RUN_ID": "t"}, 60)
    assert good["exit_code"] == 0 and "STOPPROOF_RESULT" in good["stdout"]
    assert bad["exit_code"] != 0 and "STOPPROOF_RESULT" not in bad["stdout"], "a test that does not match the lock must not run"


def test_full_chain_in_the_local_sandbox_is_labelled_as_such(local_box, tmp_path):
    ev = Pipeline(DEMO / "vulnerable_agent", ScriptedModel(), local_box, tmp_path).run()
    assert ev["baseline"]["outcome"] == "STILL ABLE TO ACT" and ev["verdict"]["outcome"] == "STOPPED" and ev["same_test_verified"]
    assert ev["execution"]["backend"] == "local-bubblewrap" and ev["baseline"]["sandbox"]["backend"] == "local-bubblewrap"
    assert any("sha256sum -c" in line for line in ev["baseline"]["sandbox"]["script"])
    assert "Executed in:  Local isolated sandbox (bubblewrap, not Nebius)" in ev["summary"]


# ---- backend selection -----------------------------------------------------------------------

def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="unknown sandbox backend"):
        make_sandbox("vmware")


def test_nebius_backend_reports_why_it_is_unavailable(monkeypatch):
    for var in ("NEBIUS_API_KEY", "NEBIUS_AI_PROJECT", "CONTREE_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SandboxUnavailable) as exc:
        make_sandbox("nebius")
    assert exc.value.code == "not_configured"
    status = backend_status()
    assert set(status) == set(BACKENDS) and status["nebius"]["ok"] is False and status["nebius"]["code"] == "not_configured"


# ---- demo API -----------------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(server, "load_dotenv", lambda **_: None)
    Pipeline(DEMO / "vulnerable_agent", ScriptedModel(), LocalSandbox(), tmp_path).run()
    return TestClient(server.create_app(tmp_path)), tmp_path


def run_id_of(tmp_path):
    return next(tmp_path.iterdir()).name


def test_ui_is_served(api):
    client, _ = api
    page = client.get("/")
    assert page.status_code == 200 and "How to verify manually" in page.text and "Edit this trace and re-verify" in page.text


def test_health_lists_backends_and_picks_the_first_ready_one(api, monkeypatch):
    client, _ = api
    status = {"nebius": {"ok": False, "label": "n", "code": "not_enabled", "detail": "d"}, "local": {"ok": True, "label": "l", "detail": "ready"}}
    monkeypatch.setattr(server, "backend_status", lambda: status)
    monkeypatch.setenv("NEBIUS_API_KEY", "x")
    body = client.get("/api/health").json()
    assert body["backends"]["nebius"]["code"] == "not_enabled" and body["default_backend"] == "local"
    assert {"vulnerable_agent", "fixed_agent", "opaque_agent"} <= set(body["repos"]) and body["model"]["ok"]


def test_stored_evidence_is_listed_loaded_and_reverified(api):
    client, tmp_path = api
    rows = client.get("/api/evidence").json()
    assert rows[0]["outcome"] == "STOPPED" and rows[0]["patched"] and rows[0]["backend"] == "test-double"
    run_id = run_id_of(tmp_path)
    assert client.get(f"/api/evidence/{run_id}").json()["run_id"] == run_id
    check = client.get(f"/api/evidence/{run_id}/reverify").json()
    assert check["ok"] and check["passed"] == check["total"] and check["total"] == 18
    assert client.get("/api/evidence/does-not-exist").status_code == 404
    assert client.get("/api/evidence/..").status_code == 404
    assert client.get("/api/metrics").json()["executed_in_nebius_sandbox"] == {"runs": 0, "of": 1}


def test_trace_endpoint_runs_the_verifier(api):
    client, tmp_path = api
    ev = json.loads((tmp_path / run_id_of(tmp_path) / "evidence.json").read_text())
    primary = ev["baseline"]["windows"][0]
    late = client.post("/api/verify", json={"run_id": "t", "events": primary["events"]}).json()
    assert late["outcome"] == "STILL ABLE TO ACT" and late["late_effects"]
    fixed = ev["after_patch"]["windows"][0]
    assert client.post("/api/verify", json={"run_id": "t", "events": fixed["events"]}).json()["outcome"] == "STOPPED"
    tampered = [e for e in fixed["events"] if e["event"] not in ("worker_blocked", "job_cancelled")]
    assert client.post("/api/verify", json={"run_id": "t", "events": tampered}).json()["outcome"] == "CANNOT ESTABLISH"
    assert client.post("/api/verify", content=b"not json").status_code == 400
    assert client.post("/api/verify", json=[1, 2]).json()["outcome"] == "CANNOT ESTABLISH"


def test_run_requests_are_validated(api):
    client, _ = api
    assert client.post("/api/runs", json={"repo": "nope", "backend": "local"}).status_code == 400
    assert client.post("/api/runs", json={"repo": "vulnerable_agent", "backend": "vmware"}).status_code == 400


def wait_for(client, job_id):
    for _ in range(300):
        state = client.get(f"/api/runs/{job_id}").json()
        if state["status"] != "running":
            return state
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_unavailable_backend_fails_the_job_with_a_fix_it_code(api, monkeypatch):
    client, _ = api

    def refuse(name):
        raise SandboxUnavailable("no permission to spawn sandbox instances", "not_enabled")

    monkeypatch.setattr(server, "make_sandbox", refuse)
    state = wait_for(client, client.post("/api/runs", json={"repo": "vulnerable_agent", "backend": "nebius"}).json()["job"])
    assert state["status"] == "error" and state["code"] == "not_enabled" and "no permission" in state["error"]


def test_a_run_started_from_the_api_produces_labelled_evidence(api, monkeypatch):
    client, tmp_path = api
    monkeypatch.setattr(server, "make_sandbox", lambda name: LocalSandbox())
    monkeypatch.setattr(server, "Nemotron", lambda: ScriptedModel())
    state = wait_for(client, client.post("/api/runs", json={"repo": "fixed_agent", "backend": "local"}).json()["job"])
    assert state["status"] == "done" and state["evidence"]["verdict"]["outcome"] == "STOPPED" and state["backend"] == "local"
    assert [s["stage"] for s in state["stages"]][0] == "analyze"
    assert len(list(tmp_path.iterdir())) == 2


# ---- independent re-verification -------------------------------------------------------------

@pytest.fixture
def evidence(tmp_path):
    return Pipeline(DEMO / "vulnerable_agent", ScriptedModel(), LocalSandbox(), tmp_path).run()


def test_reverify_accepts_a_genuine_bundle(evidence):
    report = reverify_bundle(evidence)
    assert report["ok"] and report["passed"] == report["total"] and report["total"] == 18


@pytest.mark.parametrize(
    "tamper",
    [
        lambda ev: ev["baseline"]["windows"][0]["verdict"].update(outcome="STOPPED"),   # claim a late effect was safe
        lambda ev: ev["after_patch"]["windows"][0]["events"].pop(-3),                  # silently drop a recorded event
        lambda ev: ev["test"].update(source=ev["test"]["source"] + "\n# weakened"),    # edit the locked test
        lambda ev: ev["after_patch"].update(test_sha256="0" * 64),                     # re-run used a different test
        lambda ev: ev["verdict"].update(outcome="CANNOT ESTABLISH"),                   # final verdict disagrees
    ],
)
def test_reverify_detects_tampering(evidence, tamper):
    forged = copy.deepcopy(evidence)
    tamper(forged)
    assert not reverify_bundle(forged)["ok"]


def test_verify_bundle_cli(evidence, tmp_path, capsys):
    path = tmp_path / evidence["run_id"] / "evidence.json"
    assert cli.cmd_verify_bundle(Namespace(bundle=str(path.parent))) == 0
    assert "checks passed" in capsys.readouterr().out
    forged = json.loads(path.read_text())
    forged["baseline"]["windows"][0]["verdict"]["outcome"] = "STOPPED"
    path.write_text(json.dumps(forged))
    assert cli.cmd_verify_bundle(Namespace(bundle=str(path))) == 1
    assert "FAIL" in capsys.readouterr().out


def test_verify_cli_accepts_a_bare_events_file(tmp_path, capsys):
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"events": [{"event": "stop_requested", "ts": 1}]}))
    assert cli.cmd_verify(Namespace(trace=str(trace))) == 0
    assert "CANNOT ESTABLISH" in capsys.readouterr().out
