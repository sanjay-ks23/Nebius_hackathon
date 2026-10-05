"""Background worker: consumes queued jobs and performs the side effect."""
from probe import emit
from provider import send_email


class Worker:
    def __init__(self, queue):
        self.queue = queue

    def run_until_empty(self):
        while (job := self.queue.dequeue()) is not None:
            self.handle(job)

    def handle(self, job):
        emit("worker_started", action_id=job["action_id"])
        body = job["body"].strip()
        emit("worker_processing", action_id=job["action_id"])
        send_email(job["to"], body, job["action_id"])
