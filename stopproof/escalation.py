"""Optional human-in-the-loop escalation via Tendem (remote MCP, ApiKey auth).

CANNOT ESTABLISH is an honest outcome, not a dead end: when the deterministic
verifier lacks the evidence to decide, a run can be handed to a human expert.
This submits a *review request* — Tendem's orchestrator scopes it, returns a
quote, and a human reviews only after the user approves on their side. The
escalation never changes a recorded verdict; the task reference is stored
alongside the evidence bundle as supplementary metadata.

Only the task text leaves this machine: verdict, reason, the deciding window's
event trace, and repository identity — never credentials or unrelated files.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

log = logging.getLogger("stopproof.escalation")

URL = "https://mcp.tendem.ai/mcp"
TRACE_CHARS = 6000
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


class EscalationUnavailable(RuntimeError):
    pass


def enabled() -> bool:
    return bool(os.environ.get("TENDEM_API_KEY"))


class Tendem:
    """Minimal MCP client: initialize -> tools/call. No SDK dependency."""

    def __init__(self, api_key: str | None = None, url: str = URL, opener: Any = None) -> None:
        self.key = api_key or os.environ.get("TENDEM_API_KEY")
        if not self.key:
            raise EscalationUnavailable("TENDEM_API_KEY is not set")
        self.url, self.session, self._id = url, None, 0
        self._open = opener or urllib.request.urlopen
        self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "stopproof", "version": "1.0"}})
        self._rpc("notifications/initialized")

    def _rpc(self, method: str, params: dict | None = None) -> dict | None:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not method.startswith("notifications/"):
            self._id += 1
            body["id"] = self._id
        headers = {"Authorization": f"ApiKey {self.key}", **HEADERS}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, json.dumps(body).encode(), headers)
        try:
            resp = self._open(req, timeout=30)
            self.session = self.session or resp.headers.get("mcp-session-id")
            raw = resp.read().decode().strip()
        except Exception as exc:
            raise EscalationUnavailable(f"Tendem unreachable: {type(exc).__name__}: {str(exc)[:200]}") from exc
        if not raw:
            return None
        reply = None
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    reply = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
        if reply is None:
            reply = json.loads(raw)
        if isinstance(reply, dict) and reply.get("error"):
            raise EscalationUnavailable(f"Tendem error: {reply['error']}")
        return reply

    def call(self, tool: str, arguments: dict[str, Any]) -> dict:
        reply = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        if reply is None:
            raise EscalationUnavailable("Tendem returned no response")
        return reply.get("result", reply)


def review_request_text(evidence: dict[str, Any]) -> tuple[str, str]:
    """Deterministic task text from evidence — a human reviews the trace, not our opinion of it."""
    verdict = evidence.get("verdict", {})
    repo = evidence.get("repository", {}).get("path", "unknown")
    run = evidence.get("baseline") or evidence.get("after_patch") or {}
    deciding = next((w for w in run.get("windows", []) if w["window"] == verdict.get("window")), {})
    trace = json.dumps(deciding.get("trace", deciding.get("events", [])), indent=1)[:TRACE_CHARS]
    name = f"StopProof review: {repo} agent cancellation ({run.get('outcome', verdict.get('outcome', '?'))})"[:120]
    description = (
        "StopProof verified whether an AI agent's STOP reaches downstream work. The deterministic verifier could not "
        "conclusively establish safety or unsafety from the recorded trace, so a human review is requested.\n\n"
        f"Run: {evidence.get('run_id')}   Repository: {repo}   Execution: {evidence.get('execution', {}).get('label', 'unknown')}\n"
        f"Verdict: {verdict.get('outcome')} — {verdict.get('reason')}\n"
        f"Locked race test sha256: {evidence.get('test', {}).get('sha256', 'n/a')}\n\n"
        f"Window under review: {verdict.get('window', 'n/a')}\nEvent trace (logical timestamps):\n{trace}\n\n"
        "Please review: does the trace show the agent's protected side effect could still fire after the STOP was issued, "
        "or is the evidence simply incomplete? Reply with your assessment and what additional instrumentation would decide it."
    )
    return name, description


def request_human_review(evidence: dict[str, Any], client: Tendem | None = None) -> dict[str, Any]:
    """Submit the inconclusive run to a Tendem human reviewer. Returns the task reference."""
    client = client or Tendem()
    name, description = review_request_text(evidence)
    result = client.call("create_task", {"name": name, "description": description, "conversation_id": f"stopproof-{evidence.get('run_id')}"})
    out = {"status": "requested", "tool": "create_task", "result": result}
    for key in ("task_id", "task_url"):
        if isinstance(result, dict) and key in result:
            out[key] = result[key]
    log.info("escalation_submitted run=%s", evidence.get("run_id"))
    return out
