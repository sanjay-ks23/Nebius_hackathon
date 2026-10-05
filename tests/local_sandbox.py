"""Test double for the Token Factory Sandbox: runs the same bundle + script in a local temp dir.

Used only to test StopProof's own code paths. Real verification uses stopproof.sandbox.Sandbox.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path


class LocalSandbox:
    backend = "test-double"
    label = "Development test double (not isolated)"

    def __init__(self):
        self.executions = []

    def execute(self, bundle: bytes, script: str, env: dict, timeout: int) -> dict:
        with tempfile.TemporaryDirectory(prefix="fake-sandbox-") as root:
            Path(root, "bundle.tgz").write_bytes(bundle)
            full_env = {**os.environ, **env, "ROOT": root, "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"]}
            full_env.pop("PYTHONPATH", None)
            proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full_env, cwd=root, timeout=timeout)
        self.executions.append(script)
        return {"backend": self.backend, "operation_id": f"local-{len(self.executions)}", "image": "local-double", "bundle_sha256": "n/a", "networking": "n/a",
                "exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
