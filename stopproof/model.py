"""Nemotron client (Nebius Token Factory, OpenAI-compatible API).

Every response is parsed into a pydantic schema. A response that does not
validate is an error, never a silent default. Results are cached on disk by
request hash so reruns of the same analysis cost nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Self, TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

log = logging.getLogger("stopproof.model")
T = TypeVar("T", bound=BaseModel)

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
CACHE_DIR = Path(".stopproof/cache")


class ModelError(RuntimeError):
    pass


def _extract_json(text: str) -> object:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def _want_tracing() -> bool:
    """LangSmith tracing is opt-in: every model call lands as an auditable trace, the verdict path never does."""
    return os.environ.get("LANGSMITH_TRACING", "").strip().lower() in {"1", "true", "yes"} and bool(os.environ.get("LANGSMITH_API_KEY"))


class TraceSession:
    """Hierarchical LangSmith trace for one StopProof pipeline run.

    Nests all wrapped Nemotron calls + sandbox/verifier tool spans under a single
    root RunTree, posts deterministic verifier outcomes as LangSmith feedback,
    and resolves a shareable public URL when supported. Strictly best-effort:
    observability never alters a verdict or fails a run.
    """

    def __init__(self, enabled: bool, run_id: str, repo: str, backend: str) -> None:
        self.enabled = bool(enabled)
        self.run_id = run_id
        self.repo = repo
        self.backend = backend
        self.project = os.environ.get("LANGSMITH_PROJECT", "stopproof")
        self.spans: list[dict[str, Any]] = []
        self.feedback: list[dict[str, Any]] = []
        self.ls_run_id: str | None = None
        self.trace_url: str | None = None
        self._client: Any = None
        self._root: Any = None
        self._ctx: Any = None

    def __enter__(self) -> Self:
        if not self.enabled:
            return self
        try:
            import langsmith
            from langsmith.run_trees import RunTree

            self._client = langsmith.Client()
            self._root = RunTree(
                name=f"stopproof.run ({self.repo})",
                run_type="chain",
                project_name=self.project,
                client=self._client,
                inputs={"run_id": self.run_id, "repo": self.repo, "backend": self.backend},
                tags=["stopproof", self.repo, self.backend],
                extra={"metadata": {"stopproof_run_id": self.run_id, "repo": self.repo, "backend": self.backend}},
            )
            self._root.post()
            self.ls_run_id = str(self._root.id)
            self._ctx = langsmith.tracing_context(
                parent=self._root,
                project_name=self.project,
                client=self._client,
                tags=["stopproof", self.repo],
                metadata={"stopproof_run_id": self.run_id, "repo": self.repo},
            )
            self._ctx.__enter__()
        except Exception as exc:  # noqa: BLE001 - tracing is best-effort observability
            log.warning("langsmith_session_start_failed reason=%s", exc)
            self._root = self._ctx = None
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._ctx is not None:
            try:
                self._ctx.__exit__(exc_type, exc, tb)
            except Exception as err:  # noqa: BLE001
                log.debug("langsmith_context_exit_failed reason=%s", err)

    def span(self, name: str, run_type: str = "tool", inputs: dict[str, Any] | None = None, outputs: dict[str, Any] | None = None) -> None:
        self.spans.append({"name": name, "run_type": run_type, "inputs": inputs or {}, "outputs": outputs or {}})
        if self._root is None:
            return
        try:
            child = self._root.create_child(name=name, run_type=run_type, inputs=inputs or {})
            child.post()
            child.end(outputs=outputs or {})
            child.patch()
        except Exception as exc:  # noqa: BLE001
            log.debug("langsmith_span_failed name=%s reason=%s", name, exc)

    def record_feedback(self, key: str, score: float, value: str | None = None, comment: str = "") -> None:
        self.feedback.append({"key": key, "score": score, "value": value, "comment": comment})
        if self._client is None or not self.ls_run_id:
            return
        try:
            self._client.create_feedback(self.ls_run_id, key=key, score=score, value=value, comment=comment)
        except Exception as exc:  # noqa: BLE001
            log.debug("langsmith_feedback_failed key=%s reason=%s", key, exc)

    def finish(self, verdict: dict[str, Any]) -> dict[str, Any]:
        outcome = str(verdict.get("outcome", "CANNOT ESTABLISH"))
        self.record_feedback(
            "verifier_outcome",
            score=1.0 if outcome == "STOPPED" else 0.0,
            value=outcome,
            comment=str(verdict.get("reason", ""))[:300],
        )
        if self._root is not None:
            try:
                self._root.end(outputs={"verdict": verdict, "run_id": self.run_id})
                self._root.patch()
            except Exception as exc:  # noqa: BLE001
                log.debug("langsmith_root_finish_failed reason=%s", exc)
        if self._client is not None and self.ls_run_id:
            try:
                self.trace_url = str(self._client.share_run(self.ls_run_id))
            except Exception as exc:  # noqa: BLE001
                log.debug("langsmith_share_run_failed reason=%s", exc)
        return self.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "project": self.project if self.enabled else None,
            "run_id": self.ls_run_id,
            "trace_url": self.trace_url,
            "spans": [s["name"] for s in self.spans],
            "feedback": list(self.feedback),
        }


class Nemotron:
    def __init__(self, cache: bool = True) -> None:
        key = os.environ.get("NEBIUS_API_KEY")
        if not key:
            raise ModelError("NEBIUS_API_KEY is not set")
        self.model = os.environ.get("STOPPROOF_MODEL", "nvidia/nemotron-3-super-120b-a12b")
        self.client = OpenAI(base_url=os.environ.get("NEBIUS_BASE_URL", DEFAULT_BASE_URL), api_key=key, timeout=180, max_retries=2)
        self.tracing = _want_tracing()
        if self.tracing:
            try:
                from langsmith.wrappers import wrap_openai

                self.client = wrap_openai(self.client)
            except Exception as exc:  # noqa: BLE001 - tracing is observability, never a failure mode
                log.warning("langsmith_tracing_disabled reason=%s", exc)
                self.tracing = False
        self.cache = cache
        self.calls = 0
        self.cache_hits = 0

    def _trace_meta(self, stage: str, attempt: int) -> dict[str, Any]:
        return {"langsmith_extra": {"run_name": f"stopproof.{stage}", "metadata": {"stage": stage, "attempt": attempt}}} if self.tracing else {}

    def ask(self, stage: str, system: str, user: str, schema: type[T], max_tokens: int = 8000, thinking: bool = True) -> T:
        """One structured request. `stage` is only used for logging.

        thinking=False disables the model's hidden reasoning, which otherwise can exhaust the
        token budget on code-emitting stages with tightly constrained output.
        """
        schema_json = json.dumps(schema.model_json_schema())
        messages = [
            {"role": "system", "content": f"{system}\n\nRespond with a single JSON object matching this JSON schema, and nothing else:\n{schema_json}"},
            {"role": "user", "content": user},
        ]
        key = hashlib.sha256(json.dumps([self.model, messages, thinking], sort_keys=True).encode()).hexdigest()
        cached = CACHE_DIR / f"{key}.json"
        if self.cache and cached.exists():
            self.cache_hits += 1
            log.info("model_cache_hit stage=%s", stage)
            return schema.model_validate_json(cached.read_text())

        last_error = ""
        for attempt in range(1, 4):
            log.info("model_request stage=%s model=%s attempt=%d", stage, self.model, attempt)
            started = time.monotonic()
            try:
                self.calls += 1
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages
                    + ([{"role": "user", "content": f"Your previous reply was rejected: {last_error}. Reply again with only the corrected JSON object."}] if last_error else []),
                    temperature=0.2,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                    extra_body=None if thinking else {"chat_template_kwargs": {"enable_thinking": False}},
                    **self._trace_meta(stage, attempt),
                )
                content = resp.choices[0].message.content or ""
                parsed = schema.model_validate(_extract_json(content))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc)[:600]
                log.warning("model_reply_invalid stage=%s attempt=%d error=%s", stage, attempt, last_error[:200])
                continue
            except Exception as exc:  # network / auth / quota: fail clearly, do not degrade
                raise ModelError(f"model request failed at stage {stage!r}: {type(exc).__name__}: {str(exc)[:300]}") from exc
            log.info("model_response stage=%s seconds=%.1f tokens=%s", stage, time.monotonic() - started, getattr(resp.usage, "total_tokens", "?"))
            if self.cache:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(parsed.model_dump_json())
            return parsed
        raise ModelError(f"model reply for stage {stage!r} never matched the schema: {last_error}")

    def ask_text(self, stage: str, system: str, user: str, max_tokens: int = 20000, thinking: bool = True) -> str:
        """One free-form request, for stages whose output is code.

        JSON string escaping corrupts code often enough to matter (stray brackets leak into strings),
        so code-emitting stages use plain-text blocks that the caller parses strictly. The reply is not
        trusted: anything malformed is rejected by the caller.
        """
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        key = hashlib.sha256(json.dumps([self.model, messages, thinking, "text"], sort_keys=True).encode()).hexdigest()
        cached = CACHE_DIR / f"{key}.json"
        if self.cache and cached.exists():
            self.cache_hits += 1
            log.info("model_cache_hit stage=%s", stage)
            return json.loads(cached.read_text())["text"]

        for attempt in range(1, 4):
            log.info("model_request stage=%s model=%s attempt=%d", stage, self.model, attempt)
            started = time.monotonic()
            try:
                self.calls += 1
                resp = self.client.chat.completions.create(
                    model=self.model, messages=messages, temperature=0.2, max_tokens=max_tokens,
                    extra_body=None if thinking else {"chat_template_kwargs": {"enable_thinking": False}},
                    **self._trace_meta(stage, attempt),
                )
            except Exception as exc:  # network / auth / quota: fail clearly, do not degrade
                raise ModelError(f"model request failed at stage {stage!r}: {type(exc).__name__}: {str(exc)[:300]}") from exc
            choice = resp.choices[0]
            text = re.sub(r"<think>.*?</think>", "", choice.message.content or "", flags=re.DOTALL).strip()
            log.info("model_response stage=%s seconds=%.1f tokens=%s finish=%s", stage, time.monotonic() - started, getattr(resp.usage, "total_tokens", "?"), choice.finish_reason)
            if not text or choice.finish_reason == "length":  # empty or cut off mid-reply: never act on a truncated edit list
                log.warning("model_reply_unusable stage=%s attempt=%d finish=%s", stage, attempt, choice.finish_reason)
                continue
            if self.cache:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps({"text": text}))
            return text
        raise ModelError(f"model reply for stage {stage!r} was empty or truncated on every attempt")
