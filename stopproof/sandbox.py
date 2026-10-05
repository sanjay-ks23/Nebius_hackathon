"""Execution backends: where target-repository code actually runs.

Every backend takes the same deterministic tarball (harness + repo + locked test)
plus a shell script, runs it with networking off in a fresh, disposable
environment, and returns the same result dict. Nothing persists between runs,
which is also what makes environment reset trivial.

  nebius  Nebius Token Factory Sandbox (disposable microVM). The primary path.
  local   bubblewrap namespaces on the development machine. An explicit fallback
          for when Sandboxes is not enabled for the Nebius project. It is weaker
          than a microVM (shared host kernel) and every evidence bundle records
          which backend produced it, so a local run is never mistaken for a
          Nebius-sandbox run.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .patching import LockedTest

log = logging.getLogger("stopproof.sandbox")

ROOT = "/work"
DEFAULT_IMAGE_TAG = "python:3.12-slim"
IMAGE_SOURCE = "docker://docker.io/library/python:3.12-slim"
RUNTIME_DIR = Path(__file__).parent / "runtime"
SMOKE_SCRIPT = 'cd "$ROOT" && python -c "import tarfile; tarfile.open(\'bundle.tgz\').extractall(\'.\', filter=\'data\')" && cat repo/hello.txt && python --version'


class SandboxUnavailable(RuntimeError):
    """A backend cannot run the workflow. `code` lets the UI explain how to fix it."""

    def __init__(self, message: str, code: str = "unavailable") -> None:
        super().__init__(message)
        self.code = code


def build_bundle(repo_files: dict[str, str], test: LockedTest | None) -> bytes:
    """Deterministic tar.gz: harness/ (StopProof runtime), repo/ (target), locked/ (test)."""
    entries: dict[str, bytes] = {"harness/stopproof/__init__.py": b""}
    for path in sorted(RUNTIME_DIR.glob("*.py")):
        entries[f"harness/stopproof/runtime/{path.name}"] = path.read_bytes()
    entries.update({f"repo/{p}": t.encode() for p, t in repo_files.items()})
    if test is not None:
        entries[test.path] = test.source.encode()
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for name in sorted(entries):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime, info.uid, info.gid = len(entries[name]), 0o644, 0, 0, 0
            tar.addfile(info, io.BytesIO(entries[name]))
    return buf.getvalue()


class Backend:
    backend = "unknown"
    label = "unknown"

    def ensure_ready(self) -> Any:
        raise NotImplementedError

    def execute(self, bundle: bytes, script: str, env: dict[str, str], timeout: int) -> dict[str, Any]:
        raise NotImplementedError

    def check(self) -> dict[str, Any]:
        """Connectivity, permissions, file transfer and command execution in one round trip."""
        result = self.execute(build_bundle({"hello.txt": "hi\n"}, None), SMOKE_SCRIPT, {}, 60)
        return {k: result[k] for k in ("backend", "exit_code", "stdout", "operation_id", "image")}


class Sandbox(Backend):
    """Nebius Token Factory Sandbox (Contree)."""

    backend = "nebius-sandbox"
    label = "Nebius Token Factory Sandbox (microVM)"

    def __init__(self) -> None:
        from contree_client.httpx import ContreeClient  # imported lazily: only needed for real runs

        token = os.environ.get("NEBIUS_API_KEY")
        project = os.environ.get("NEBIUS_AI_PROJECT") or os.environ.get("CONTREE_PROJECT")
        if not token or not project:
            raise SandboxUnavailable("NEBIUS_API_KEY and NEBIUS_AI_PROJECT must be set to use the Token Factory Sandbox", "not_configured")
        self.client = ContreeClient(token, project=project, base_url=os.environ.get("CONTREE_URL", "https://api.tokenfactory.nebius.com/sandboxes"), timeout=300.0)
        self.tag = os.environ.get("STOPPROOF_SANDBOX_IMAGE", DEFAULT_IMAGE_TAG)
        self._image: str | None = None

    def ensure_ready(self) -> dict[str, bool]:
        """Fail fast, before any model tokens are spent, if this token cannot spawn sandbox instances."""
        from contree_client.exceptions import ContreeError

        try:
            perms = self.client.whoami().permissions
        except ContreeError as exc:
            raise SandboxUnavailable(f"sandbox not reachable: {exc}", "unreachable") from exc
        if not (perms.get("spawn_disposable") or perms.get("spawn")):
            raise SandboxUnavailable(
                f"this token/project has no permission to spawn sandbox instances (Sandboxes is a Beta feature and must be enabled for the project); permissions={perms}",
                "not_enabled",
            )
        return perms

    def image(self) -> str:
        if self._image:
            return self._image
        from contree_client.exceptions import NotFoundError
        from contree_client.models import ImageImportRegistry

        try:
            self._image = self.client.resolve_image(self.tag)
        except NotFoundError:
            log.info("sandbox_image_import tag=%s", self.tag)
            self._image = str(self.client.import_image(ImageImportRegistry(url=IMAGE_SOURCE), tag=self.tag, timeout=300))
        return self._image

    def execute(self, bundle: bytes, script: str, env: dict[str, str], timeout: int) -> dict[str, Any]:
        from contree_client.exceptions import ContreeError
        from contree_client.models import FileSpec, InstanceNetworking, OperationStatus

        bundle_sha = hashlib.sha256(bundle).hexdigest()
        stored = self.client.ensure_file(bundle)
        image = self.image()
        done = None
        # The Beta API can drop or time out mid-operation; a second try spawns a
        # fresh disposable microVM, which is safe because runs are self-contained.
        for attempt in range(2):
            try:
                log.info("sandbox_spawn image=%s bundle=%s timeout=%ss attempt=%s", image, bundle_sha[:12], timeout, attempt + 1)
                op = self.client.spawn_instance(
                    script, image, shell=True, disposable=True, timeout=timeout,
                    env={**env, "ROOT": ROOT}, cwd=ROOT,
                    networking=InstanceNetworking(enabled=False),
                    files={f"{ROOT}/bundle.tgz": FileSpec(uuid=stored.uuid, mode="0644")},
                )
                done = self.client.wait_operation(op.uuid, timeout=timeout + 120)
                break
            except (ContreeError, TimeoutError) as exc:
                if attempt:
                    raise SandboxUnavailable(f"sandbox execution failed: {type(exc).__name__}: {str(exc)[:300]}", "execution_failed") from exc
                log.warning("sandbox_spawn_retry error=%s", str(exc)[:200])
        if done.status is not OperationStatus.SUCCESS:
            raise SandboxUnavailable(f"sandbox operation {op.uuid} ended as {done.status}: {done.error}", "execution_failed")
        res = done.metadata.result
        out = {
            "backend": self.backend, "operation_id": op.uuid, "image": image, "bundle_sha256": bundle_sha, "networking": "disabled",
            "exit_code": res.state.exit_code, "stdout": res.stdout.as_bytes().decode("utf-8", "replace"),
            "stderr": res.stderr.as_bytes().decode("utf-8", "replace"),
        }
        log.info("sandbox_done op=%s exit=%s", op.uuid, out["exit_code"])
        return out


class BubblewrapSandbox(Backend):
    """Local fallback: the script runs inside bubblewrap namespaces on this machine.

    Isolation: new user/pid/ipc/uts/net namespaces (no network interface at all), every
    capability dropped, an unprivileged uid, a read-only view of /usr only (no home
    directory, no /mnt, no /etc, no secrets), and a scratch tmpfs as the only writable
    place. CPU time, file size and address space are capped. It dies with its parent.
    Weaker than a microVM: the host kernel is shared.
    """

    backend = "local-bubblewrap"
    label = "Local isolated sandbox (bubblewrap, not Nebius)"
    python = "/usr/bin/python3"
    limits = "ulimit -t 100; ulimit -f 65536; ulimit -v 4194304\n"
    _verified = False

    def ensure_ready(self) -> None:
        if BubblewrapSandbox._verified:
            return
        if not shutil.which("bwrap"):
            raise SandboxUnavailable("bubblewrap is not installed (apt install bubblewrap)", "not_installed")
        if not os.path.exists(self.python):
            raise SandboxUnavailable(f"{self.python} is required inside the local sandbox", "not_installed")
        probe = self._run(b"", "python --version", {}, 20)
        if probe["exit_code"] != 0:
            raise SandboxUnavailable(f"bubblewrap cannot create an isolated environment here: {probe['stderr'].strip()[:200]}", "unavailable")
        BubblewrapSandbox._verified = True

    def _command(self, fd: int, script: str, env: dict[str, str]) -> list[str]:
        args = [
            "bwrap", "--die-with-parent", "--new-session", "--unshare-all", "--cap-drop", "ALL",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/sbin", "/sbin",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", ROOT,
            "--file", str(fd), f"{ROOT}/bundle.tgz", "--symlink", self.python, "/opt/bin/python",
            "--uid", "65534", "--gid", "65534", "--chdir", ROOT, "--clearenv",
        ]
        for key, value in {"PATH": "/opt/bin:/usr/bin:/bin", "HOME": ROOT, "LANG": "C.UTF-8", **env, "ROOT": ROOT}.items():
            args += ["--setenv", key, value]
        return [*args, "bash", "-c", self.limits + script]

    def _run(self, bundle: bytes, script: str, env: dict[str, str], timeout: int) -> dict[str, Any]:
        bundle_sha = hashlib.sha256(bundle).hexdigest()
        with tempfile.TemporaryFile() as payload:
            payload.write(bundle)
            payload.seek(0)
            try:
                proc = subprocess.run(self._command(payload.fileno(), script, env), pass_fds=[payload.fileno()], capture_output=True, timeout=timeout)
                code, out, err = proc.returncode, proc.stdout, proc.stderr
            except subprocess.TimeoutExpired as exc:
                code, out, err = 124, exc.stdout or b"", (exc.stderr or b"") + f"\ntimed out after {timeout}s".encode()
        return {
            "backend": self.backend, "operation_id": f"bwrap-{uuid.uuid4().hex[:8]}", "image": "host python3, read-only /usr", "bundle_sha256": bundle_sha,
            "networking": "disabled", "exit_code": code, "stdout": out.decode("utf-8", "replace"), "stderr": err.decode("utf-8", "replace"),
        }

    def execute(self, bundle: bytes, script: str, env: dict[str, str], timeout: int) -> dict[str, Any]:
        self.ensure_ready()
        log.info("sandbox_spawn backend=%s bundle=%s timeout=%ss", self.backend, hashlib.sha256(bundle).hexdigest()[:12], timeout)
        result = self._run(bundle, script, env, timeout)
        log.info("sandbox_done op=%s exit=%s", result["operation_id"], result["exit_code"])
        return result


BACKENDS: dict[str, type[Backend]] = {"nebius": Sandbox, "local": BubblewrapSandbox}


def make_sandbox(name: str) -> Backend:
    """Builds a backend and verifies it can run, or raises SandboxUnavailable with a fix-it code."""
    if name not in BACKENDS:
        raise ValueError(f"unknown sandbox backend {name!r}; choose one of {sorted(BACKENDS)}")
    runner = BACKENDS[name]()
    runner.ensure_ready()
    return runner


def backend_status() -> dict[str, dict[str, Any]]:
    """Availability of every backend, for the UI and `stopproof check`."""
    status: dict[str, dict[str, Any]] = {}
    for name, cls in BACKENDS.items():
        try:
            make_sandbox(name)
            status[name] = {"ok": True, "label": cls.label, "detail": "ready"}
        except SandboxUnavailable as exc:
            status[name] = {"ok": False, "label": cls.label, "code": exc.code, "detail": str(exc)}
    return status
