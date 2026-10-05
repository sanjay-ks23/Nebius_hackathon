"""A small agent that drafts an email and hands the send to a background queue."""
from probe import emit


def draft_email(task):
    # "Send Alice an email: the meeting is cancelled" -> ("alice@example.com", "the meeting is cancelled")
    head, _, body = task.partition(":")
    name = head.split()[1] if len(head.split()) > 1 else "nobody"
    return f"{name.lower()}@example.com", body.strip()


class Agent:
    def __init__(self, agent_id, queue):
        self.agent_id = agent_id
        self.queue = queue
        self.cancelled = False
        self._next_action = 1

    def cancel(self):
        self.cancelled = True
        emit("agent_cancelled")

    def run(self, task):
        emit("agent_started")
        to, body = draft_email(task)
        action = {
            "action_id": f"email-{self._next_action}",
            "type": "send_email",
            "to": to,
            "body": body,
            "origin": self.agent_id,
        }
        self._next_action += 1
        emit("action_created", action_id=action["action_id"])
        if self.cancelled:
            return None
        self.queue.enqueue(action)
        return action
