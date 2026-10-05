"""Optional Tavily grounding: real documentation snippets inside model prompts.

Analysis and repair should be grounded in a library's actual cancellation
semantics, not whatever the model happens to remember. When TAVILY_API_KEY is
set, a small search runs once per run (cached on disk) and the snippets are
injected into the relevant prompts. Without the key every helper returns "" —
nothing else changes, and intel never influences the verdict.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("stopproof.intel")

URL = "https://api.tavily.com/search"
CACHE = Path(".stopproof/cache/intel")
MAX_SNIPPETS = 3
SNIPPET_CHARS = 400


def enabled() -> bool:
    return bool(os.environ.get("TAVILY_API_KEY"))


def search(query: str) -> list[str]:
    """'title — snippet (url)' lines, or [] when disabled/unreachable."""
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return []
    digest = hashlib.sha256(query.encode()).hexdigest()[:16]
    cached = CACHE / f"{digest}.json"
    if cached.exists():
        return json.loads(cached.read_text())
    try:
        req = urllib.request.Request(
            URL,
            json.dumps({"api_key": key, "query": query, "max_results": MAX_SNIPPETS, "search_depth": "basic"}).encode(),
            {"Content-Type": "application/json"},
        )
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        hits = [
            f"{r.get('title', '').strip()} — {str(r.get('content', ''))[:SNIPPET_CHARS].strip()} ({r.get('url', '')})"
            for r in data.get("results", [])[:MAX_SNIPPETS]
        ]
    except (OSError, ValueError) as exc:  # grounding is an enhancement; never fail the pipeline for it
        log.warning("intel_search_failed query=%r error=%s", query, str(exc)[:200])
        return []
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(hits))
    return hits


def _block(title: str, hits: list[str]) -> str:
    return f"{title}\n" + "\n".join(f"- {h}" for h in hits) + "\n" if hits else ""


def analysis_context(scan: dict[str, Any]) -> str:
    """Docs for third-party libraries the target repo actually imports."""
    deps = [d for d in scan.get("dependencies", {}).get("third_party_imports", []) if d not in {"stopproof"}]
    hits = [h for dep in deps[:2] for h in search(f"{dep} python cancel task revoke job worker semantics")[:1]]
    return _block("Documentation for libraries this repository uses (authoritative, not to be guessed):", hits)


def repair_context(feedback: str) -> str:
    """On a failed repair attempt, fetch the real idiom instead of letting the model retry blind."""
    hits = search("python worker check cancellation flag before executing queued job pattern")
    if "queue" in feedback or "cancel" in feedback:
        hits += search("python persistent cancelled flag shared between processes job queue")
    return _block("Documentation and patterns relevant to the rejection below:", hits[:MAX_SNIPPETS])
