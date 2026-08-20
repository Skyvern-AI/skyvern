"""Repeated-failure tracking for the copilot agent loop.

Computes two normalized keys per run:

- **failure signature**: "is this the same failure as last time?" —
  structural root-cause identity when available, otherwise normalized failure
  reason + top failure category + suspicious-success flag.
- **frontier fingerprint**: SHA256 of the executed blocks' canonical config.
  Changes whenever the agent edits any block in the executed suffix.

A streak counter increments only when BOTH keys repeat. It resets on:
- a meaningful-data success
- a different frontier fingerprint
- a different failure signature

Enforcement uses the streak count to escalate nudges (see ``enforcement.py``).
This module does not itself decide when to stop the loop — it only maintains
the state enforcement reads.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from skyvern.forge.sdk.copilot.challenge_evidence import ANTI_BOT_CHALLENGE_ALIAS_CATEGORIES

_FAILURE_REASON_MAX_CHARS = 200
_ROOT_CAUSE_SIGNATURE_VERSION = "repair_root_cause:v1"

# Stable identifier for the per-tool budget failure category written into
# ``failure_categories`` by the watchdog. Used by enforcement, reconciliation,
# and signature normalization as a single source of truth.
PER_TOOL_BUDGET_FAILURE_CATEGORY = "PER_TOOL_BUDGET"
ANTI_BOT_CHALLENGE_ROOT_CAUSE_CATEGORY = "ANTI_BOT_CHALLENGE"

# Stable active-run terminal evidence identifiers. The category is stored in
# tool result ``failure_categories``; the reason code is stored on blocker
# signals that convert that tool result into product-safe final copy.

_ROOT_CAUSE_CATEGORY_ALIASES = {
    **{alias: ANTI_BOT_CHALLENGE_ROOT_CAUSE_CATEGORY for alias in ANTI_BOT_CHALLENGE_ALIAS_CATEGORIES},
    # Self-mappings document categories whose noisy failure prose must not
    # affect repeat identity.
    "PARAMETER_BINDING_ERROR": "PARAMETER_BINDING_ERROR",
    PER_TOOL_BUDGET_FAILURE_CATEGORY: PER_TOOL_BUDGET_FAILURE_CATEGORY,
}
ANTI_BOT_CHALLENGE_FAILURE_CATEGORIES = frozenset(
    {
        ANTI_BOT_CHALLENGE_ROOT_CAUSE_CATEGORY,
        *(
            category
            for category, normalized in _ROOT_CAUSE_CATEGORY_ALIASES.items()
            if normalized == ANTI_BOT_CHALLENGE_ROOT_CAUSE_CATEGORY
        ),
    }
)
_CATEGORY_ONLY_ROOT_CAUSES = frozenset(
    {
        ANTI_BOT_CHALLENGE_ROOT_CAUSE_CATEGORY,
        "PARAMETER_BINDING_ERROR",
        PER_TOOL_BUDGET_FAILURE_CATEGORY,
    }
)
_LOCATOR_RE = re.compile(r"""(?:page\.)?locator\(\s*(["'])(?P<selector>.+?)\1""")
_SELECTOR_QUOTED_RE = re.compile(
    r"""(?:selector|css selector|target selector|resolved selector)\s*[:=]\s*(["'])(?P<selector>.+?)\1""",
    re.IGNORECASE,
)
_SELECTOR_UNQUOTED_RE = re.compile(
    r"""(?:selector|css selector|target selector|resolved selector)\s*[:=]\s*(?P<selector>[#.\[\]:=\w\-/]+)""",
    re.IGNORECASE,
)
_ROLE_RE = re.compile(
    r"""get_by_role\(\s*(["'])(?P<role>.+?)\1(?:\s*,\s*name\s*=\s*(["'])(?P<name>.*?)\3)?""",
    re.IGNORECASE,
)
_EXCEPTION_CLASS_RE = re.compile(r"""\b(?P<class>[A-Za-z_][\w.]*?(?:Error|Exception))\s*:""")
# ``browser_context.mode=none`` is the stable enum-backed value surfaced by
# local browser-state failures, unlike surrounding natural-language prose.
_BROWSER_SESSION_ERROR_RE = re.compile(
    r"\b(browser session not found|no browser context|no active browser|browser_context\.mode=none|"
    r"no_active_browser|browser_not_found)\b",
    re.IGNORECASE,
)
_TIMEOUT_ERROR_RE = re.compile(r"\b(timeout(?:error)?|timed out)\b", re.IGNORECASE)


@dataclass(frozen=True)
class RepairRootCauseIdentity:
    root_cause_signature: str | None = None
    primary_category: str = ""
    failure_categories: tuple[str, ...] = ()
    error_class: str = ""
    selector_kind: str = ""
    selector: str = ""


def normalize_failure_reason(raw: str | None) -> str:
    if not raw:
        return ""
    collapsed = " ".join(raw.split())
    if len(collapsed) > _FAILURE_REASON_MAX_CHARS:
        collapsed = collapsed[:_FAILURE_REASON_MAX_CHARS]
    return collapsed.lower()


def _category_name(value: Mapping[str, Any] | str) -> str:
    raw = value.get("category") if isinstance(value, Mapping) else value
    return str(raw or "").strip().upper()


def _normalized_root_cause_categories(
    failure_categories: Sequence[Mapping[str, Any] | str] | None,
    detected_challenge: bool,
) -> tuple[str, ...]:
    categories: set[str] = set()
    for entry in failure_categories or ():
        category = _category_name(entry)
        if not category:
            continue
        categories.add(_ROOT_CAUSE_CATEGORY_ALIASES.get(category, category))
    if detected_challenge:
        categories.add(ANTI_BOT_CHALLENGE_ROOT_CAUSE_CATEGORY)
    return tuple(sorted(categories))


def _normalize_signature_text(value: str) -> str:
    return " ".join(value.strip().split()).rstrip(".,;")


def _selector_from_text(text: str) -> tuple[str, str]:
    for pattern, kind in (
        (_LOCATOR_RE, "locator"),
        (_SELECTOR_QUOTED_RE, "selector"),
        (_SELECTOR_UNQUOTED_RE, "selector"),
    ):
        match = pattern.search(text)
        if match:
            return kind, _normalize_signature_text(match.group("selector"))
    role_match = _ROLE_RE.search(text)
    if role_match:
        role = _normalize_signature_text(role_match.group("role"))
        name = _normalize_signature_text(role_match.group("name") or "")
        return "role", f"{role}:{name}" if name else role
    return "", ""


_CSS_SELECTOR_KIND = "locator"


def selector_identity_from_failure(text: str) -> str:
    kind, value = _selector_from_text(text or "")
    # "selector" and "locator" are the same CSS string under two spellings; the code side only ever
    # spells it "locator", so a failure that named it "selector" must normalize or never match.
    return f"{_CSS_SELECTOR_KIND if kind == 'selector' else kind}:{value}" if kind else ""


def selector_identities_in_text(text: str) -> set[str]:
    """Every locator identity in ``text``, normalized to match ``selector_identity_from_failure``."""
    identities: set[str] = set()
    for match in _LOCATOR_RE.finditer(text or ""):
        identities.add(f"{_CSS_SELECTOR_KIND}:{_normalize_signature_text(match.group('selector'))}")
    for match in _ROLE_RE.finditer(text or ""):
        role = _normalize_signature_text(match.group("role"))
        name = _normalize_signature_text(match.group("name") or "")
        identities.add(f"role:{role}:{name}" if name else f"role:{role}")
    return identities


def _error_class_from_text(text: str) -> str:
    if _BROWSER_SESSION_ERROR_RE.search(text):
        return "browser_session_not_found"
    match = _EXCEPTION_CLASS_RE.search(text)
    if match:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", match.group("class").split(".")[-1]).lower()
    if _TIMEOUT_ERROR_RE.search(text):
        return "timeout_error"
    return ""


def _root_cause_texts(
    failure_reason: str | None,
    error_texts: Sequence[str | None] | None,
    blocks: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    texts = [text for text in ([failure_reason] + list(error_texts or ())) if isinstance(text, str) and text.strip()]
    for block in blocks or ():
        for key in ("failure_reason", "error", "message"):
            value = block.get(key)
            if isinstance(value, str) and value.strip():
                texts.append(value)
    return texts


def compute_repair_root_cause_signature(
    *,
    failure_categories: Sequence[Mapping[str, Any] | str] | None = None,
    failure_reason: str | None = None,
    error_texts: Sequence[str | None] | None = None,
    blocks: Sequence[Mapping[str, Any]] | None = None,
    detected_challenge: bool = False,
) -> RepairRootCauseIdentity:
    categories = _normalized_root_cause_categories(failure_categories, detected_challenge)
    primary_category = categories[0] if categories else ""
    texts = _root_cause_texts(failure_reason, error_texts, blocks)
    error_class = ""
    selector_kind = ""
    selector = ""
    for text in texts:
        if not error_class:
            error_class = _error_class_from_text(text)
        if not selector:
            selector_kind, selector = _selector_from_text(text)
        if error_class and selector:
            break

    if set(categories) & _CATEGORY_ONLY_ROOT_CAUSES:
        error_class = ""
        selector_kind = ""
        selector = ""
    if not categories and not error_class and not selector:
        return RepairRootCauseIdentity()

    payload = {
        "version": _ROOT_CAUSE_SIGNATURE_VERSION,
        "failure_categories": categories,
        "error_class": error_class,
        "selector_kind": selector_kind,
        "selector": selector,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signature = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return RepairRootCauseIdentity(
        root_cause_signature=signature,
        primary_category=primary_category,
        failure_categories=categories,
        error_class=error_class,
        selector_kind=selector_kind,
        selector=selector,
    )


def _canonical_block_config(block: Any) -> dict[str, Any]:
    """Stable dict view of a block's material config, with fields that don't
    affect downstream behavior (``output_parameter``) dropped.
    """
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        try:
            cfg = dump(mode="json", exclude_none=True)
        except TypeError:
            cfg = dump()
    elif isinstance(block, dict):
        cfg = dict(block)
    else:
        return {"repr": repr(block)}
    cfg.pop("output_parameter", None)
    return cfg
