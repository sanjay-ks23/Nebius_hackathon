"""Mock email provider: the protected sink.

The only way an email is "sent" is through this module, and the provider_send
event is emitted here, outside any file a repair patch may touch. A patch can
change whether the worker reaches the provider; it cannot hide that it did.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import emit


@dataclass
class MockEmailProvider:
    outbox: list[dict[str, str]] = field(default_factory=list)

    def send(self, to: str, body: str, action_id: str) -> None:
        self.outbox.append({"to": to, "body": body, "action_id": action_id})
        emit("provider_send", action_id=action_id, to=to)
