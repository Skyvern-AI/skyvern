"""Relevance filtering of scraped prose via the ZettaQuant V-SLM API.

EVALUATION ONLY -- vendor proposal under review, not intended for merge. The
vendor call is unconditionally gated on ENABLE_VSLM_TEXT_FILTER (default False)
and fails open, so a disabled or broken filter leaves the caller's text intact.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from skyvern.config import settings
from skyvern.utils.token_counter import count_tokens

LOG = structlog.get_logger()

# The API rejects payloads above this; the vendor guide calls 512 the most
# efficient batch size (fewest round-trips, no token-aware splitting).
_MAX_SENTENCES_PER_CALL = 512
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Sentence split on terminal punctuation followed by whitespace, plus hard
# newlines: scraped innerText is line-oriented and often has no punctuation at
# all (nav chrome, table cells), which a punctuation-only split would glue into
# one enormous "sentence".
_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass
class VSLMStats:
    """What one filter pass did, for logging and A/B measurement."""

    enabled: bool = False
    applied: bool = False
    sentences_in: int = 0
    sentences_kept: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    api_calls: int = 0
    api_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def token_reduction(self) -> float:
        return 0.0 if not self.tokens_in else 1.0 - (self.tokens_out / self.tokens_in)

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "vslm_applied": self.applied,
            "vslm_sentences_in": self.sentences_in,
            "vslm_sentences_kept": self.sentences_kept,
            "vslm_tokens_in": self.tokens_in,
            "vslm_tokens_out": self.tokens_out,
            "vslm_token_reduction": round(self.token_reduction, 4),
            "vslm_api_calls": self.api_calls,
            "vslm_api_seconds": round(self.api_seconds, 3),
            "vslm_errors": self.errors or None,
        }


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SPLIT_RE.split(text) if s.strip()]


def schema_to_query(schema: dict[str, Any] | None) -> str:
    """Flatten a JSON schema's field names and descriptions into a topic string.

    FileParserBlock has no natural-language goal, only a target schema, so the
    schema's own vocabulary is the only relevance signal available.
    """
    if not schema:
        return ""
    parts: list[str] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if desc := node.get("description"):
            parts.append(str(desc))
        props = node.get("properties")
        if isinstance(props, dict):
            for name, child in props.items():
                parts.append(str(name).replace("_", " "))
                walk(child)
        if isinstance(node.get("items"), dict):
            walk(node["items"])

    walk(schema)
    seen: set[str] = set()
    ordered = [p for p in parts if not (p.lower() in seen or seen.add(p.lower()))]
    return ", ".join(ordered)


async def _predict_batch(client: httpx.AsyncClient, sentences: list[str], topic: str) -> list[str]:
    """One /v1/vslm/predict call, with the vendor's documented retry policy.

    5xx and 429 retry with exponential backoff; 4xx never does.
    """
    last_error: Exception | None = None
    for attempt in range(settings.VSLM_MAX_ATTEMPTS):
        try:
            response = await client.post(
                f"{settings.ZQ_BASE_URL.rstrip('/')}/v1/vslm/predict",
                headers={"x-api-key": settings.ZQ_API_KEY, "Content-Type": "application/json"},
                json={"agent": settings.VSLM_AGENT, "topic": topic, "sentences": sentences},
            )
            if response.status_code in _RETRYABLE_STATUS:
                raise httpx.HTTPStatusError(
                    f"retryable status {response.status_code}", request=response.request, response=response
                )
            response.raise_for_status()
            kept = response.json().get("relevant_sentences")
            if not isinstance(kept, list):
                raise ValueError("relevant_sentences missing from response")
            return [str(s) for s in kept]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _RETRYABLE_STATUS:
                raise
            last_error = exc
        except (httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
            last_error = exc
        if attempt + 1 < settings.VSLM_MAX_ATTEMPTS:
            await asyncio.sleep(settings.VSLM_BACKOFF_SECONDS * (2**attempt))
    assert last_error is not None
    raise last_error


async def filter_text_for_relevance(text: str | None, topic: str | None) -> tuple[str | None, VSLMStats]:
    """Drop sentences the V-SLM classifier judges irrelevant to ``topic``.

    Fails open: on any error, or when the flag/key/topic is missing, returns the
    input text unchanged so a vendor outage can never block extraction.
    """
    stats = VSLMStats(enabled=settings.ENABLE_VSLM_TEXT_FILTER)
    if not text or not stats.enabled or not settings.ZQ_API_KEY or not (topic or "").strip():
        return text, stats

    sentences = split_sentences(text)
    stats.sentences_in = len(sentences)
    stats.tokens_in = count_tokens(text)
    if not sentences:
        return text, stats

    batches = [sentences[i : i + _MAX_SENTENCES_PER_CALL] for i in range(0, len(sentences), _MAX_SENTENCES_PER_CALL)]
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=settings.VSLM_TIMEOUT_SECONDS) as client:
            results = await asyncio.gather(*(_predict_batch(client, b, topic.strip()) for b in batches))
        stats.api_calls = len(batches)
    except Exception as exc:
        stats.api_seconds = time.monotonic() - started
        stats.errors.append(f"{type(exc).__name__}: {exc}")
        LOG.warning("vslm_filter_failed_open", error=str(exc), sentences=len(sentences))
        return text, stats
    stats.api_seconds = time.monotonic() - started

    # Rejoin in original order. The API echoes sentences back rather than
    # indices, so membership is by exact string; duplicates collapse to one
    # decision, which is what we want for repeated boilerplate.
    kept_set = {s for batch in results for s in batch}
    kept = [s for s in sentences if s in kept_set]
    if not kept:
        stats.errors.append("empty_result")
        LOG.warning("vslm_filter_kept_nothing", sentences=len(sentences), topic=topic[:120])
        return text, stats

    filtered = "\n".join(kept)
    stats.applied = True
    stats.sentences_kept = len(kept)
    stats.tokens_out = count_tokens(filtered)
    return filtered, stats
