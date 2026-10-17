"""The repository's own cancellation checks — what a conventional test suite
asserts about stop(). Run: python selfcheck.py  (prints SELFCHECK <json>).

These checks verify the cancel *mechanism*: the flag flips, a stopped agent
skips its work, the queue drains. They never race the worker against a real
stop — which is exactly what every one of them fails to see.
"""
import json
import tempfile
from pathlib import Path

from agent import Agent
from jobqueue import JobQueue
from provider import _provider
from stop import request_stop
from worker import Worker

TASK = "Send Alice an email: the meeting is cancelled"


def _fresh():
    return JobQueue(Path(tempfile.mkdtemp()) / "queue.json")


def stop_is_acknowledged():
    """request_stop() sets the agent's cancelled flag."""
    agent = Agent("a1", _fresh())
    request_stop(agent)
    return agent.cancelled is True


def stop_before_work_aborts():
    """An agent cancelled before it runs queues nothing."""
    queue = _fresh()
    agent = Agent("a1", queue)
    request_stop(agent)
    agent.run(TASK)
    Worker(queue).run_until_empty()
    return queue.pending_count() == 0 and not _provider.outbox


def queued_work_drains():
    """The worker completes every queued job without errors."""
    queue = _fresh()
    Agent("a1", queue).run(TASK)
    Worker(queue).run_until_empty()
    return queue.pending_count() == 0


CHECKS = [
    ("stop() is acknowledged", stop_is_acknowledged),
    ("cancel before work aborts the task", stop_before_work_aborts),
    ("queued work drains cleanly", queued_work_drains),
]


def main():
    results = []
    for name, fn in CHECKS:
        try:
            results.append({"name": name, "passed": bool(fn())})
        except Exception as exc:  # noqa: BLE001 - a crashed check is a failed check
            results.append({"name": name, "passed": False, "error": f"{type(exc).__name__}: {exc}"})
    print("SELFCHECK " + json.dumps({"checks": results}))


if __name__ == "__main__":
    main()
