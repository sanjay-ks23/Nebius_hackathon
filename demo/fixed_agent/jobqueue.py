"""A file-backed job queue. Jobs survive the process that created them."""
import json
from pathlib import Path

from probe import emit


class JobQueue:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.exists():
            self._save({"jobs": [], "cancelled_origins": []})

    def _load(self):
        return json.loads(self.path.read_text())

    def _save(self, state):
        self.path.write_text(json.dumps(state))

    def enqueue(self, job):
        state = self._load()
        state["jobs"].append(job)
        self._save(state)
        emit("job_enqueued", action_id=job["action_id"])

    def dequeue(self):
        state = self._load()
        if not state["jobs"]:
            return None
        job = state["jobs"].pop(0)
        self._save(state)
        return job

    def pending_count(self):
        return len(self._load()["jobs"])

    def cancel_origin(self, origin):
        """Cancel every queued job created by `origin` and remember the cancellation."""
        state = self._load()
        state["cancelled_origins"].append(origin)
        kept = []
        for job in state["jobs"]:
            if job["origin"] == origin:
                emit("job_cancelled", action_id=job["action_id"])
            else:
                kept.append(job)
        state["jobs"] = kept
        self._save(state)

    def is_cancelled(self, job):
        return job["origin"] in self._load()["cancelled_origins"]
