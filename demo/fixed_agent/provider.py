"""Email provider adapter. The mock records the protected effect; when
STOPPROOF_SINK_URL is set (live-demo mode on the host only — sandboxes run
with networking disabled) each send also performs a real outbound delivery.
Sinks are comma-separated: file:// writes to disk, anything else is POSTed
(e.g. an ntfy.sh topic).
"""
import os
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from stopproof.runtime import MockEmailProvider

_provider = MockEmailProvider()


def _post(url, message):
    """POST with one retry — notification services are flaky, and a transient
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
        try:
            target = urlparse(url)
            if target.scheme == "file":
                path = Path(target.path)
                if url.endswith("/"):
                    path.mkdir(parents=True, exist_ok=True)
                    path = path / f"{action_id}-{os.environ.get('STOPPROOF_LIVE_LABEL', 'live')}.txt"
                path.write_text(message)
            else:
                _post(url, message)
        except OSError as exc:
            # One dead sink must not starve the others — report and move on.
            print(f"[live-sink] {url[:48]} delivery failed for {action_id}: {exc}", file=sys.stderr)


def send_email(to, body, action_id):
    _provider.send(to, body, action_id)
    url = os.environ.get("STOPPROOF_SINK_URL")
    if url:
        try:
            _deliver(url, f"EMAIL SENT id={action_id} to={to} body={body!r} window={os.environ.get('STOPPROOF_LIVE_LABEL', '?')}", action_id)
        except OSError as exc:
            # The push mirror is demo theatre, not the protected effect — the
            # recorded provider_send stands; a sink outage must not taint the trace.
            print(f"[live-sink] delivery failed for {action_id}: {exc}", file=sys.stderr)
