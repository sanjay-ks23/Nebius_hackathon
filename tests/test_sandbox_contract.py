"""Contract test for stopproof.sandbox.Sandbox using contree_client's in-memory client (same interface as the real one)."""
import base64
import io
import tarfile
from types import SimpleNamespace

import pytest
from contree_client.exceptions import ContreeError
from contree_client.models import (
    InstanceResult,
    InstanceResultState,
    InstanceSpawnResponse,
    OperationInstanceMetadata,
    OperationResponse,
    OperationStatus,
    StreamRepr,
)
from contree_client.testing import ContreeClient

from stopproof.sandbox import ROOT, Sandbox, SandboxUnavailable, build_bundle


def make_sandbox(client):
    box = Sandbox.__new__(Sandbox)
    box.client, box.tag, box._image = client, "python:3.12-slim", None
    return box


def finished(status=OperationStatus.SUCCESS, exit_code=0, stdout="hello\n", stderr=""):
    def stream(text):
        return StreamRepr(value=base64.b64encode(text.encode()).decode(), encoding="base64")

    result = InstanceResult(state=InstanceResultState(exit_code=exit_code), stdout=stream(stdout), stderr=stream(stderr))
    return OperationResponse(uuid="op-1", status=status, error=None if status is OperationStatus.SUCCESS else "boom",
                             metadata=OperationInstanceMetadata(command="x", image="img", result=result))


def mocked(done):
    client = ContreeClient()
    client.mock("resolve_image", "img-uuid")
    client.mock("ensure_file", SimpleNamespace(uuid="file-uuid"))
    client.mock("spawn_instance", InstanceSpawnResponse(uuid="op-1"))
    client.mock("wait_operation", done)
    return client


def test_execute_spawns_a_disposable_offline_vm_with_the_bundle_and_returns_output():
    client = mocked(finished(stdout="STOPPROOF_RESULT {}\n"))
    out = make_sandbox(client).execute(build_bundle({"a.py": "x = 1\n"}, None), "echo hi", {"RUN_ID": "r1"}, 60)
    assert out["exit_code"] == 0 and out["stdout"] == "STOPPROOF_RESULT {}\n" and out["operation_id"] == "op-1" and out["networking"] == "disabled"
    call = client.calls_for("spawn_instance")[0]
    assert call.args[1] == "img-uuid" and call.kwargs["disposable"] is True and call.kwargs["shell"] is True
    assert call.kwargs["networking"].enabled is False
    assert call.kwargs["env"] == {"RUN_ID": "r1", "ROOT": ROOT}
    assert list(call.kwargs["files"]) == [f"{ROOT}/bundle.tgz"]


def test_bundle_is_deterministic_and_contains_harness_repo_and_locked_test():
    from stopproof.patching import lock_test

    test = lock_test("from a import A\nfrom stopproof.runtime.races import Scenario, main\ndef build(w):\n    return Scenario(A, A, A)\nmain(build)\n", {"a"})
    one, two = build_bundle({"a.py": "class A: ...\n"}, test), build_bundle({"a.py": "class A: ...\n"}, test)
    assert one == two
    with tarfile.open(fileobj=io.BytesIO(one)) as tf:
        names = tf.getnames()
    assert {"harness/stopproof/runtime/events.py", "harness/stopproof/runtime/races.py", "repo/a.py", "locked/race_test.py"} <= set(names)


@pytest.mark.parametrize("failure", [TimeoutError("slow"), ContreeError("api down")])
def test_infrastructure_failures_are_errors_not_verdicts(failure):
    client = ContreeClient()
    client.mock("ensure_file", SimpleNamespace(uuid="f"))
    client.mock("resolve_image", "img")
    client.mock("spawn_instance", error=failure)
    with pytest.raises(SandboxUnavailable) as exc:
        make_sandbox(client).execute(b"", "true", {}, 5)
    assert exc.value.code == "execution_failed"


def test_non_success_operation_is_an_error():
    with pytest.raises(SandboxUnavailable, match="FAILED"):
        make_sandbox(mocked(finished(status=OperationStatus.FAILED))).execute(b"", "true", {}, 5)


@pytest.mark.parametrize("perms, ok", [({"spawn": False, "spawn_disposable": False}, False), ({"spawn": False, "spawn_disposable": True}, True)])
def test_ensure_ready_fails_fast_without_spawn_permission(perms, ok):
    client = ContreeClient()
    client.mock("whoami", SimpleNamespace(permissions=perms))
    box = make_sandbox(client)
    if ok:
        assert box.ensure_ready() == perms
    else:
        with pytest.raises(SandboxUnavailable, match="no permission") as exc:
            box.ensure_ready()
        assert exc.value.code == "not_enabled"
