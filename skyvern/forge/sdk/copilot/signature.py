"""Bounded, deterministic reply signatures for cross-turn loop detection.

The signature is computed over the user-visible reply (post
``normalize_response_scaffolding``), with variable spans (URLs, Skyvern IDs,
datetimes, numbers, backtick-quoted identifiers) stripped so two replies that
differ only by run-specific values share a signature.
"""

from __future__ import annotations

import hashlib
import re

from skyvern.forge.sdk.copilot.request_policy import redact_raw_secrets_for_prompt

RAW_SLICE_CAP = 8_000
COMPARE_CAP = 4_000
_DIGEST_BYTES = 8

_WHITESPACE_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SKYVERN_ID_RE = re.compile(
    r"\b(?:wpid|wcc|wccm|wr|w|tsk|o|block|step|cred|wf)_[A-Za-z0-9]+",
    re.IGNORECASE,
)
_ISO_DATETIME_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+\-]\d{2}:?\d{2})?)?\b")
_NUMERIC_RUN_RE = re.compile(r"\b\d{3,}\b")
_BACKTICK_IDENT_RE = re.compile(r"`[A-Za-z][A-Za-z0-9_]*`")


def normalize_for_compare(text: str) -> str:
    raw = text or ""
    if len(raw) > RAW_SLICE_CAP:
        half = RAW_SLICE_CAP // 2
        raw = raw[:half] + raw[-half:]
    redacted = redact_raw_secrets_for_prompt(raw)
    for pattern in (_URL_RE, _SKYVERN_ID_RE, _ISO_DATETIME_RE, _BACKTICK_IDENT_RE, _NUMERIC_RUN_RE):
        redacted = pattern.sub(" ", redacted)
    collapsed = _WHITESPACE_RE.sub(" ", redacted).strip().lower()
    return collapsed[:COMPARE_CAP]


def compute_signature(text: str) -> str:
    digest = hashlib.blake2b(normalize_for_compare(text).encode("utf-8"), digest_size=_DIGEST_BYTES)
    return digest.hexdigest()
