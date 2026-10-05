"""Live integration tests against the configured Nebius services (need credentials in the environment / .env).

Inference tests spend a few hundred tokens. Sandbox tests are skipped, with the reason shown, when the
token/project is not permitted to spawn sandbox instances.
"""
from pathlib import Path

import pytest
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

from stopproof.model import ModelError, Nemotron  # noqa: E402
from stopproof.orchestrator import Pipeline  # noqa: E402
from stopproof.sandbox import Sandbox  # noqa: E402

DEMO = Path(__file__).resolve().parent.parent / "demo"


class Ping(BaseModel):
    ok: bool
    word: str


@pytest.fixture(scope="module")
def sandbox():
    try:
        box = Sandbox()
        perms = box.client.whoami().permissions
    except Exception as exc:
        pytest.skip(f"sandbox not reachable: {exc}")
    if not (perms.get("spawn_disposable") or perms.get("spawn")):
        pytest.skip("Sandbox token/project lacks spawn permission (Sandboxes is a Beta feature that must be enabled for the project)")
    return box


def test_nemotron_returns_schema_valid_json():
    try:
        model = Nemotron(cache=False)
    except ModelError as exc:
        pytest.skip(str(exc))
    reply = model.ask("ping", "You answer with JSON only.", 'Return {"ok": true, "word": "stop"}', Ping, max_tokens=500, thinking=False)
    assert reply.ok and reply.word


def test_sandbox_file_transfer_command_execution_and_isolation(sandbox):
    result = sandbox.check()
    assert result["exit_code"] == 0 and "hi" in result["stdout"] and "Python 3.12" in result["stdout"]
    again = sandbox.execute(b"", "test ! -e /work/leftover && echo clean", {}, 60)
    assert "clean" in again["stdout"]


@pytest.mark.parametrize("repo, outcome, patched", [("vulnerable_agent", "STOPPED", True), ("fixed_agent", "STOPPED", False), ("opaque_agent", "CANNOT ESTABLISH", False)])
def test_full_chain_in_the_real_sandbox(sandbox, tmp_path, repo, outcome, patched):
    ev = Pipeline(DEMO / repo, Nemotron(), sandbox, tmp_path).run()
    assert not ev.get("error"), ev.get("error")
    assert ev["verdict"]["outcome"] == outcome
    assert bool(ev["patch"]["applied"]) is patched
    if repo == "vulnerable_agent":
        assert ev["baseline"]["outcome"] == "STILL ABLE TO ACT" and ev["same_test_verified"]
    assert ev["baseline"]["sandbox"]["image"] != "local-double"
