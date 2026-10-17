"""Optional integrations: Tavily grounding, Tendem escalation, LangSmith tracing,
and the host-side live demo with a real sink. None of them may influence a verdict."""
import json
from pathlib import Path

import pytest

from stopproof import escalation, intel, livedemo
from stopproof.model import Nemotron
from stopproof.verifier import Outcome

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"


# ---- Tavily intel --------------------------------------------------------------

def test_intel_disabled_without_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert intel.enabled() is False
    assert intel.search("anything") == []
    assert intel.analysis_context({"dependencies": {"third_party_imports": ["celery"]}}) == ""


def test_intel_search_formats_hits(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(intel, "CACHE", tmp_path)

    class Resp:
        def read(self):
            return json.dumps({"results": [{"title": "Celery revoke", "content": "revoke() cancels queued tasks", "url": "https://x/y"}]}).encode()

    monkeypatch.setattr(intel.urllib.request, "urlopen", lambda req, timeout: Resp())
    hits = intel.search("celery cancel task")
    assert hits == ["Celery revoke — revoke() cancels queued tasks (https://x/y)"]
    assert intel.search("celery cancel task") == hits  # second call comes from disk cache


def test_intel_failure_is_silent(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(intel, "CACHE", tmp_path)

    def boom(req, timeout):
        raise OSError("offline")

    monkeypatch.setattr(intel.urllib.request, "urlopen", boom)
    assert intel.search("anything") == []  # grounding failure never fails the pipeline


def test_repair_context_builds_block(monkeypatch):
    monkeypatch.setattr(intel, "search", lambda q: ["cancel docs — check the flag (https://x)"])
    block = intel.repair_context("the locked test still fails: provider_send after stop_requested in queue")
    assert "cancel docs" in block and block.startswith("Documentation and patterns")
    monkeypatch.setattr(intel, "search", lambda q: [])
    assert intel.repair_context("some rejection") == ""


# ---- LangSmith tracing ----------------------------------------------------------

def test_tracing_off_without_flag(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "k")
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    m = Nemotron(cache=False)
    assert m.tracing is False and m._trace_meta("x", 1) == {}


def test_tracing_wraps_client(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "k")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsk")
    m = Nemotron(cache=False)
    assert m.tracing is True
    meta = m._trace_meta("repair", 2)
    assert meta["langsmith_extra"]["run_name"] == "stopproof.repair"
    assert meta["langsmith_extra"]["metadata"] == {"stage": "repair", "attempt": 2}


# ---- Tendem escalation ----------------------------------------------------------

class _Resp:
    def __init__(self, body=b"", session=None):
        self._body, self.headers = body, ({"mcp-session-id": session} if session else {})

    def read(self):
        return self._body


def _opener(replies):
    calls = []

    def open_(req, timeout):
        calls.append(json.loads(req.data))
        return _Resp(replies[len(calls) - 1] if len(calls) <= len(replies) else replies[-1], session="s1")

    return calls, open_


def _sse(result):
    return f'data: {json.dumps({"result": result})}\n\n'.encode()


def test_tendem_handshake_and_create_task():
    replies = [_sse({"serverInfo": {"name": "tendem-mcp"}}), b"", _sse({"task_id": "abc", "task_url": "https://agent.tendem.ai/t/abc"})]
    calls, open_ = _opener(replies)
    client = escalation.Tendem(api_key="k", opener=open_)
    result = client.call("create_task", {"name": "n", "description": "d"})
    assert result["task_url"].endswith("/abc")
    assert calls[0]["method"] == "initialize" and calls[1]["method"] == "notifications/initialized" and calls[2]["method"] == "tools/call"


def test_escalation_needs_key(monkeypatch):
    monkeypatch.delenv("TENDEM_API_KEY", raising=False)
    with pytest.raises(escalation.EscalationUnavailable):
        escalation.Tendem()


def test_review_text_carries_verdict_and_trace():
    evidence = {"run_id": "run-x", "repository": {"path": "opaque_agent"}, "execution": {"label": "test"},
                "verdict": {"outcome": "CANNOT ESTABLISH", "reason": "no worker telemetry", "window": "queue_stop_worker"},
                "baseline": {"outcome": "CANNOT ESTABLISH", "windows": [{"window": "queue_stop_worker", "events": [{"event": "agent_started", "ts": 1000}]}]}}
    name, description = escalation.review_request_text(evidence)
    assert "opaque_agent" in name and "CANNOT ESTABLISH" in description and "agent_started" in description


# ---- live demo on the host ------------------------------------------------------

def _load_provider(monkeypatch, repo="vulnerable_agent"):
    import importlib
    import sys

    sys.modules.pop("provider", None)  # never reuse a copy cached by another test
    monkeypatch.syspath_prepend(str(DEMO / repo))
    return importlib.import_module("provider")


def test_multi_sink_fans_out_to_every_target(monkeypatch, tmp_path):
    provider = _load_provider(monkeypatch)
    monkeypatch.setenv("STOPPROOF_LIVE_LABEL", "testwin")
    a, b = tmp_path / "a", tmp_path / "b"
    provider._deliver(f"file://{a}/,file://{b}/", "msg", "email-1")
    assert (a / "email-1-testwin.txt").read_text() == "msg"
    assert (b / "email-1-testwin.txt").read_text() == "msg"


def test_mailto_sink_sends_real_smtp(monkeypatch):
    provider = _load_provider(monkeypatch)
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["conn"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr(provider.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_PASS", "app-password")
    provider._smtp_send("them@example.com", "EMAIL SENT id=email-1", "email-1")
    assert sent["conn"] == ("smtp.gmail.com", 465)
    assert sent["login"] == ("me@gmail.com", "app-password")
    assert sent["msg"]["To"] == "them@example.com"
    assert "EMAIL SENT" in sent["msg"]["Subject"]


def test_mailto_sink_requires_credentials(monkeypatch):
    provider = _load_provider(monkeypatch)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    with pytest.raises(RuntimeError):
        provider._smtp_send("them@example.com", "x", "email-1")


def test_live_demo_vulnerable_delivers_after_stop(tmp_path):
    res = livedemo.run_live(DEMO / "vulnerable_agent", f"file://{tmp_path}/")
    assert res["outcome"] == Outcome.STILL_ABLE_TO_ACT.value
    late = [w for w in res["windows"] if w["verdict"]["outcome"] == "STILL ABLE TO ACT"]
    assert len(late) == 4
    delivered = sorted(tmp_path.glob("*.txt"))
    assert len(delivered) == 5  # 4 late + the legitimate pre-STOP send in stop_after_commit


def test_live_demo_fixed_delivers_only_before_stop(tmp_path):
    res = livedemo.run_live(DEMO / "fixed_agent", f"file://{tmp_path}/")
    assert res["outcome"] == Outcome.STOPPED.value
    delivered = list(tmp_path.glob("*.txt"))
    assert len(delivered) == 1 and "stop_after_commit" in delivered[0].name


def test_repo_selfchecks_all_pass_even_on_vulnerable():
    """The 'without StopProof' story: conventional checks pass on every agent,
    broken included — green tests are not proof."""
    for repo in ("vulnerable_agent", "fixed_agent", "opaque_agent"):
        checks = livedemo.run_selfcheck(DEMO / repo)
        assert checks and all(c["passed"] for c in checks), repo
