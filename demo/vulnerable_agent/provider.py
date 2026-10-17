"""Email provider adapter. The mock records the protected effect; when
STOPPROOF_SINK_URL is set (live-demo mode on the host only — sandboxes run
with networking disabled) each send also performs a real outbound delivery.
Sinks are comma-separated: file:// writes to disk, mailto:addr sends a real
SMTP email (SMTP_HOST/PORT/USER/PASS env), anything else is POSTed (e.g. ntfy.sh).
"""
import os
import smtplib
import sys
import time
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse

from stopproof.runtime import MockEmailProvider

_provider = MockEmailProvider()


def _smtp_send(to_addr, message, action_id):
    user, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    if not (user and password):
        raise RuntimeError("mailto: sink needs SMTP_USER and SMTP_PASS in the environment")
    msg = EmailMessage()
    msg["From"], msg["To"] = user, to_addr
    msg["Subject"] = f"StopProof live proof — EMAIL SENT {action_id}"
    msg.set_content(f"{message}\n\nThis real email was delivered by the agent during a StopProof live run.")
    with smtplib.SMTP_SSL(
        os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465")), timeout=15
    ) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


def _post(url, message):
    """POST with two retries — notification services are flaky, and a transient
    flap must not corrupt the demonstration. A persistent failure still raises."""
    last = None
    for attempt in range(2):
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=message.encode()), timeout=4).close()
            return
        except (urllib.error.URLError, OSError) as exc:
            last = exc
            if attempt < 1:
                time.sleep(attempt + 1)
    raise last


def _deliver(urls, message, action_id):
    for url in urls.split(","):
        url = url.strip()
        if not url:
            continue
        target = urlparse(url)
        if target.scheme == "file":
            path = Path(target.path)
            if url.endswith("/"):
                path.mkdir(parents=True, exist_ok=True)
                path = path / f"{action_id}-{os.environ.get('STOPPROOF_LIVE_LABEL', 'live')}.txt"
            path.write_text(message)
        elif target.scheme == "mailto":
            _smtp_send(target.path, message, action_id)
        else:
            _post(url, message)


def send_email(to, body, action_id):
    _provider.send(to, body, action_id)
    url = os.environ.get("STOPPROOF_SINK_URL")
    if url:
        try:
            _deliver(url, f"EMAIL SENT id={action_id} to={to} body={body!r} window={os.environ.get('STOPPROOF_LIVE_LABEL', '?')}", action_id)
        except (OSError, RuntimeError, smtplib.SMTPException) as exc:
            # The push/email mirror is demo theatre, not the protected effect —
            # the recorded provider_send stands; a sink outage must not taint the trace.
            print(f"[live-sink] delivery failed for {action_id}: {exc}", file=sys.stderr)
