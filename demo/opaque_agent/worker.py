"""Background worker: consumes queued jobs and performs the side effect."""
from provider import send_email


class Worker:
    def __init__(self, queue):
        self.queue = queue

    def run_until_empty(self):
        while (job := self.queue.dequeue()) is not None:
            self.handle(job)

    def handle(self, job):
        if self.queue.is_cancelled(job):
            return
        send_email(job["to"], job["body"].strip(), job["action_id"])
