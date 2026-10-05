"""Email provider adapter. In this demo the provider is StopProof's mock."""
from stopproof.runtime import MockEmailProvider

_provider = MockEmailProvider()


def send_email(to, body, action_id):
    _provider.send(to, body, action_id)
