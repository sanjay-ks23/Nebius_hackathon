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
from typing import Any, TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

log = logging.getLogger("stopproof.model")
T = TypeVar("T", bound=BaseModel)

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
CACHE_DIR = Path(".stopproof/cache")


class ModelError(RuntimeError):
    pass


def _extract_json(text: str) -> object:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
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
            except Exception as exc:  # tracing is observability, never a failure mode
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
            text = re.sub(r"<think>.*?</think>", "", choice.message.content or "", flags=re.S).strip()
            log.info("model_response stage=%s seconds=%.1f tokens=%s finish=%s", stage, time.monotonic() - started, getattr(resp.usage, "total_tokens", "?"), choice.finish_reason)
            if not text or choice.finish_reason == "length":  # empty or cut off mid-reply: never act on a truncated edit list
                log.warning("model_reply_unusable stage=%s attempt=%d finish=%s", stage, attempt, choice.finish_reason)
                continue
            if self.cache:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps({"text": text}))
            return text
        raise ModelError(f"model reply for stage {stage!r} was empty or truncated on every attempt")
