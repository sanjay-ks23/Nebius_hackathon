import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REFERENCE_TEST = ROOT / "tests" / "reference_race.py"


def run_race_locally(repo: Path, test: Path = REFERENCE_TEST) -> dict:
    """Runs the race test against a demo repo in a local subprocess.

    Local execution is only for developing StopProof itself; the verification
    pipeline always runs targets inside the Token Factory Sandbox.
    """
    repo = repo.resolve()
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), str(repo)]), "PYTHONHASHSEED": "0"}
    proc = subprocess.run(
        [sys.executable, str(test), "--run-id", "test"], capture_output=True, text=True, env=env, cwd=repo, timeout=120
    )
    line = next((l for l in proc.stdout.splitlines() if l.startswith("STOPPROOF_RESULT ")), None)
    assert line, proc.stderr[-2000:]
    return json.loads(line[len("STOPPROOF_RESULT "):])


@pytest.fixture(scope="session")
def demo_results():
    return {name: run_race_locally(ROOT / "demo" / name) for name in ("vulnerable_agent", "fixed_agent", "opaque_agent")}
