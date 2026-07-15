"""Shared LLM helpers (token usage, pricing, JSON parsing)."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from config import settings

logger = logging.getLogger("xfusion-backend.llm_common")

ALLOWED_INSIGHT_MODELS = frozenset({
    "gpt-4.1",
    "gpt-4o",
    "gpt-4.1-mini",
    "gpt-4o-mini",
    "gpt-4.1-nano",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
})

MODEL_PRICING_PER_1M = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-5.5": (5.00, 30.00),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
}


def resolve_insight_model(requested: Optional[str]) -> str:
    candidate = (requested or settings.DEFAULT_INSIGHT_MODEL or "gpt-4o-mini").strip()
    if candidate not in ALLOWED_INSIGHT_MODELS:
        logger.warning("Unsupported insight model %r — falling back to default.", candidate)
        return settings.DEFAULT_INSIGHT_MODEL or "gpt-4o-mini"
    return candidate


def _usage_value(source: Any, *keys: str) -> int:
    if source is None:
        return 0
    if isinstance(source, dict):
        for key in keys:
            if key in source and source[key] is not None:
                return int(source[key] or 0)
        return 0
    for key in keys:
        value = getattr(source, key, None)
        if value is not None:
            return int(value or 0)
    if hasattr(source, "model_dump"):
        dumped = source.model_dump()
        for key in keys:
            if key in dumped and dumped[key] is not None:
                return int(dumped[key] or 0)
    return 0


def extract_token_counts(response: Any, callback: Any = None) -> tuple[int, int, int]:
    prompt = completion = total = 0

    usage = getattr(response, "usage_metadata", None)
    if usage:
        prompt = _usage_value(usage, "prompt_tokens", "input_tokens")
        completion = _usage_value(usage, "completion_tokens", "output_tokens")
        total = _usage_value(usage, "total_tokens")

    if total <= 0:
        meta = getattr(response, "response_metadata", None) or {}
        if isinstance(meta, dict):
            token_usage = meta.get("token_usage") or meta.get("usage") or {}
            if token_usage:
                prompt = _usage_value(token_usage, "prompt_tokens", "input_tokens")
                completion = _usage_value(token_usage, "completion_tokens", "output_tokens")
                total = _usage_value(token_usage, "total_tokens")

    if callback is not None:
        cb_prompt = int(getattr(callback, "prompt_tokens", 0) or 0)
        cb_completion = int(getattr(callback, "completion_tokens", 0) or 0)
        cb_total = int(getattr(callback, "total_tokens", 0) or 0)
        if cb_total > 0 and total <= 0:
            prompt, completion, total = cb_prompt, cb_completion, cb_total

    if total <= 0 and (prompt > 0 or completion > 0):
        total = prompt + completion

    return prompt, completion, total


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    input_rate, output_rate = MODEL_PRICING_PER_1M.get(model, MODEL_PRICING_PER_1M["gpt-4o-mini"])
    usd = (prompt_tokens / 1_000_000) * input_rate + (completion_tokens / 1_000_000) * output_rate
    return max(0.0, round(usd, 6))


def parse_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    return data
