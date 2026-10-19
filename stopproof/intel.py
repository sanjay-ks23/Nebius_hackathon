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

_provenance: list[dict[str, Any]] = []


def enabled() -> bool:
    return bool(os.environ.get("TAVILY_API_KEY"))


def reset_provenance() -> None:
    """Clear recorded lookups at the start of a pipeline run."""
    _provenance.clear()


def provenance() -> dict[str, Any]:
    """Auditable summary of documentation lookups performed during the current run."""
    return {
        "enabled": enabled() or bool(_provenance),
        "queries_run": len(_provenance),
        "snippets_injected": sum(len(item["hits"]) for item in _provenance),
        "lookups": list(_provenance),
    }


def search(query: str, *, depth: str = "basic", include_answer: bool = False) -> list[str]:
    """'title — snippet (url)' lines, or [] when disabled/unreachable."""
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return []
    cache_input = query if (depth == "basic" and not include_answer) else f"{depth}:{include_answer}:{query}"
    digest = hashlib.sha256(cache_input.encode()).hexdigest()[:16]
    cached = CACHE / f"{digest}.json"
    if cached.exists():
        return json.loads(cached.read_text())
    payload: dict[str, Any] = {"api_key": key, "query": query, "max_results": MAX_SNIPPETS, "search_depth": depth}
    if include_answer:
        payload["include_answer"] = True
    try:
        req = urllib.request.Request(
            URL,
            json.dumps(payload).encode(),
            {"Content-Type": "application/json"},
        )
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        hits: list[str] = []
        if include_answer and data.get("answer"):
            hits.append(f"Tavily synthesis — {str(data['answer'])[:SNIPPET_CHARS].strip()}")
        hits.extend(
            f"{r.get('title', '').strip()} — {str(r.get('content', ''))[:SNIPPET_CHARS].strip()} ({r.get('url', '')})"
            for r in data.get("results", [])[:MAX_SNIPPETS]
        )
        hits = hits[:MAX_SNIPPETS]
    except (OSError, ValueError) as exc:  # grounding is an enhancement; never fail the pipeline for it
        log.warning("intel_search_failed query=%r error=%s", query, str(exc)[:200])
        return []
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(hits))
    return hits


def _lookup(stage: str, query: str, *, depth: str = "basic", include_answer: bool = False) -> list[str]:
    try:
        hits = search(query) if (depth == "basic" and not include_answer) else search(query, depth=depth, include_answer=include_answer)
    except TypeError:  # test doubles may monkeypatch search(q) with a 1-arg callable
        hits = search(query)
    if hits:
        _provenance.append({"stage": stage, "query": query, "depth": depth, "hits": list(hits)})
    return list(hits)


def _block(title: str, hits: list[str]) -> str:
    return f"{title}\n" + "\n".join(f"- {h}" for h in hits) + "\n" if hits else ""


def analysis_context(scan: dict[str, Any]) -> str:
    """Docs for third-party libraries and sink semantics the target repo actually uses."""
    deps = [d for d in scan.get("dependencies", {}).get("third_party_imports", []) if d not in {"stopproof"}]
    hits = [h for dep in deps[:2] for h in _lookup("analyze", f"{dep} python cancel task revoke job worker semantics")[:1]]
    sinks = sorted({s.get("call", "") for s in scan.get("sinks", []) if s.get("call")})
    if sinks:
        hits += _lookup("analyze", f"python {sinks[0]} idempotent side effect cancellation boundary")[:1]
    return _block("Documentation for libraries this repository uses (authoritative, not to be guessed):", hits[:MAX_SNIPPETS])


def test_context(scan: dict[str, Any], found: dict[str, Any]) -> str:
    """Grounding for deterministic race-test generation around the detected queue and worker."""
    deps = [d for d in scan.get("dependencies", {}).get("third_party_imports", []) if d not in {"stopproof"}]
    handoff = found.get("handoff", {}).get("symbol", "enqueue")
    worker = found.get("worker_entry", {}).get("symbol", "worker")
    query = " ".join(["python", *deps[:1], "deterministic test", handoff, worker, "cancel before side effect"])
    hits = _lookup("generate_test", query)
    return _block("Documentation and patterns for testing queue-to-worker cancellation:", hits[:MAX_SNIPPETS])


def repair_context(feedback: str, found: dict[str, Any] | None = None) -> str:
    """On a failed repair attempt, fetch the real idiom instead of letting the model retry blind."""
    hits = _lookup("repair", "python worker check cancellation flag before executing queued job pattern", depth="advanced", include_answer=True)
    if "queue" in feedback or "cancel" in feedback:
        hits += _lookup("repair", "python persistent cancelled flag shared between processes job queue", depth="advanced", include_answer=True)
    if found and found.get("worker_entry", {}).get("symbol"):
        sym = found["worker_entry"]["symbol"]
        hits += _lookup("repair", f"python {sym} re-check cancellation guard immediately before side effect", depth="advanced")
    return _block("Documentation and patterns relevant to the rejection below:", hits[:MAX_SNIPPETS])
