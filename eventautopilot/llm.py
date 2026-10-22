"""LLM switch: Grok Bot (tier 1) -> xAI Grok API (tier 2) -> template (tier 3, caller's job).

complete_json never raises; on any failure it returns (None, "template") so the
caller falls back to deterministic templates.
"""
from __future__ import annotations

import json
import logging
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from . import config

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
TIMEOUT = 40.0


def _grok_bot(system: str, user: str, schema: type[BaseModel]) -> BaseModel | None:
    """Tier 1: Grok Bot.

    TODO(Q1): Grok Bot is an agent product and we found NO public API for it.
    If the partner gives us a callable interface, implement it here and return
    a validated `schema` instance. Until then this returns None and the switch
    falls through to the xAI API.
    """
    return None


def _chat(system: str, user: str, model: str) -> str | None:
    """One OpenAI-compatible chat completion; returns message text or None."""
    return _chat_messages([{"role": "system", "content": system},
                           {"role": "user", "content": user}], model)


def _chat_messages(messages: list[dict[str, str]], model: str) -> str | None:
    try:
        r = httpx.post(
            f"{config.GROK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {config.GROK_API_KEY}"},
            json={"model": model, "messages": messages, "temperature": 0.4,
                  "response_format": {"type": "json_object"}},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:  # network, HTTP, malformed body: degrade, never raise
        log.warning("Grok API call failed: %s", e)
        return None


def _parse(text: str, schema: type[T]) -> T:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[4:] if t.lower().startswith("json") else t
    return schema.model_validate_json(t.strip())


def complete_json(system: str, user: str, schema: type[T], fast: bool = True) -> tuple[T | None, str]:
    """Return (parsed, tier_used). tier_used is 'grokbot', 'grok' or 'template'."""
    try:
        got = _grok_bot(system, user, schema)
        if got is not None:
            return got, "grokbot"  # type: ignore[return-value]
    except Exception as e:
        log.warning("Grok Bot failed: %s", e)

    if not config.HAS_GROK:
        return None, "template"

    model = config.GROK_MODEL_FAST if fast else config.GROK_MODEL_SMART
    sys_full = (f"{system}\n\nRespond with a single JSON object matching this JSON Schema:\n"
                f"{json.dumps(schema.model_json_schema())}")
    messages = [{"role": "system", "content": sys_full}, {"role": "user", "content": user}]
    for attempt in range(2):
        text = _chat_messages(messages, model)
        if text is None:
            return None, "template"
        try:
            return _parse(text, schema), "grok"
        except (ValidationError, ValueError) as e:
            if attempt == 1:
                break
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"That failed validation:\n{e}\nReturn corrected JSON only."},
            ]
    return None, "template"
