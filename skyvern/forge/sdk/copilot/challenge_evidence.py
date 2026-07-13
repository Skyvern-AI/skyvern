"""Typed positive-evidence carrier required to assert any anti-bot challenge
category; keyword hits in prose, HTML haystacks, action traces, or code-block
values are not carriers, and a category without one is untrusted."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

import structlog

LOG = structlog.get_logger()


class ChallengeEvidenceSource(StrEnum):
    CHALLENGE_STATE = "challenge_state"
    VISION = "vision"
    ARTIFACT = "artifact"
    KEYWORD_ONLY = "keyword_only"


CHALLENGE_EVIDENCE_SOURCE_KEY = "evidence_source"
CARRIER_CHALLENGE_EVIDENCE_SOURCES: frozenset[ChallengeEvidenceSource] = frozenset(
    {
        ChallengeEvidenceSource.CHALLENGE_STATE,
        ChallengeEvidenceSource.VISION,
        ChallengeEvidenceSource.ARTIFACT,
    }
)

ANTI_BOT_CHALLENGE_ALIAS_CATEGORIES: frozenset[str] = frozenset(
    {
        "ANTI_BOT_CHALLENGE",
        "ANTI_BOT_DETECTION",
        "CHALLENGE_DETECTION",
        "HUMAN_VERIFICATION_CHALLENGE",
    }
)

# The vision classifier's typed non-challenge obstruction kind: a consent dialog
# is dismissed, not solved, so it must never promote challenge state.
CONSENT_OBSTRUCTION_KIND = "cookie_consent"

# Tags that carry challenge-vendor markup without rendering a widget. A passive
# script/meta tag ships on every page behind some CDNs, so it can trigger the
# visual fallback but never assert human verification by itself.
_PASSIVE_CHALLENGE_TAGS: frozenset[str] = frozenset({"script", "noscript", "style", "link", "meta"})

# Exact-match boolean flag keys in run artifacts that count as a typed challenge
# marker; substring key matches and status-string values never do.
ANTI_BOT_ARTIFACT_FLAG_KEYS: frozenset[str] = frozenset(
    {
        "anti_bot",
        "anti_bot_blocked",
        "anti_bot_detected",
        "blocked_by_captcha",
        "blocked_by_challenge",
        "bot_blocked",
        "bot_detected",
        "bot_detection",
        "captcha",
        "captcha_blocked",
        "captcha_detected",
        "captcha_present",
        "captcha_required",
        "challenge",
        "challenge_blocked",
        "challenge_detected",
        "challenge_present",
        "challenge_required",
        "human_verification",
        "human_verification_blocked",
        "human_verification_detected",
        "human_verification_required",
    }
)
# Exact-match enumerated marker values (whole normalized string, never a
# substring): the same markers when an artifact encodes them as an identifier
# value instead of a boolean flag. Single bare tokens and generic values like
# "blocked" or "challenge" never match as values.
ANTI_BOT_ARTIFACT_MARKER_VALUES: frozenset[str] = frozenset(
    {
        "anti_bot_blocked",
        "anti_bot_detected",
        "blocked_by_captcha",
        "blocked_by_challenge",
        "bot_blocked",
        "bot_detected",
        "browser_or_environment_port_block",
        "browser_port_forbidden",
        "captcha_blocked",
        "captcha_detected",
        "captcha_required",
        "challenge_blocked",
        "challenge_detected",
        "challenge_required",
        "human_verification_blocked",
        "human_verification_detected",
        "human_verification_required",
    }
)
_MAX_ARTIFACT_FLAG_DEPTH = 5


def interactive_challenge_controls(challenge_controls: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        control
        for control in challenge_controls or []
        if isinstance(control, dict) and str(control.get("tag") or "").lower() not in _PASSIVE_CHALLENGE_TAGS
    ]


def _category_name(entry: object) -> str:
    raw = entry.get("category") if isinstance(entry, Mapping) else entry
    return str(raw or "").strip().upper()


def challenge_evidence_source_from_entry(entry: Mapping[str, Any]) -> ChallengeEvidenceSource | None:
    raw = entry.get(CHALLENGE_EVIDENCE_SOURCE_KEY)
    if not isinstance(raw, str):
        return None
    try:
        return ChallengeEvidenceSource(raw.strip().lower())
    except ValueError:
        return None


def is_carrier_backed_category_entry(entry: object) -> bool:
    """True for every non-anti-bot category; anti-bot aliases require a carrier
    ``evidence_source`` and an absent or unknown value fails closed."""
    if _category_name(entry) not in ANTI_BOT_CHALLENGE_ALIAS_CATEGORIES:
        return True
    if not isinstance(entry, Mapping):
        return False
    return challenge_evidence_source_from_entry(entry) in CARRIER_CHALLENGE_EVIDENCE_SOURCES


def carrier_backed_anti_bot_categories(value: object) -> list[Any]:
    """Drop anti-bot alias category entries that are not carrier-backed; every
    other entry passes through unchanged, preserving order."""
    if not isinstance(value, list):
        return []
    kept: list[Any] = []
    for entry in value:
        if is_carrier_backed_category_entry(entry):
            kept.append(entry)
            continue
        suppressed_source = challenge_evidence_source_from_entry(entry) if isinstance(entry, Mapping) else None
        LOG.info(
            "copilot anti-bot category keyword-only-suppressed",
            category=_category_name(entry),
            suppressed_evidence_source=suppressed_source.value if suppressed_source else "absent",
        )
    return kept


def first_carrier_backed_anti_bot_source(value: object) -> ChallengeEvidenceSource | None:
    if not isinstance(value, list):
        return None
    for entry in value:
        if not isinstance(entry, Mapping) or _category_name(entry) not in ANTI_BOT_CHALLENGE_ALIAS_CATEGORIES:
            continue
        source = challenge_evidence_source_from_entry(entry)
        if source in CARRIER_CHALLENGE_EVIDENCE_SOURCES:
            return source
    return None


def composition_challenge_carrier(evidence: Mapping[str, Any] | None) -> ChallengeEvidenceSource | None:
    """Carrier observed in composition page evidence: a rendered interactive
    challenge control, a stamped carrier on ``challenge_state``, or an asserted
    ``requires_human_verification``. Raw indicator hits alone return ``None``."""
    if not isinstance(evidence, Mapping):
        return None
    controls = evidence.get("challenge_controls")
    if isinstance(controls, list) and interactive_challenge_controls(controls):
        return ChallengeEvidenceSource.CHALLENGE_STATE
    challenge_state = evidence.get("challenge_state")
    if not isinstance(challenge_state, Mapping):
        return None
    stamped = challenge_evidence_source_from_entry(challenge_state)
    if stamped in CARRIER_CHALLENGE_EVIDENCE_SOURCES:
        return stamped
    # gates_submit_controls is only ever derived from rendered controls or a
    # vision confirmation, so it carries the same weight as either.
    if (
        challenge_state.get("requires_human_verification") is True
        or challenge_state.get("gates_submit_controls") is True
    ):
        return ChallengeEvidenceSource.CHALLENGE_STATE
    return None


def vision_challenge_carrier(visual_summary: Mapping[str, Any] | None) -> bool:
    if not isinstance(visual_summary, Mapping) or visual_summary.get("challenge_detected") is not True:
        return False
    obstruction_kind = str(visual_summary.get("obstruction_kind") or "").strip().lower()
    return obstruction_kind != CONSENT_OBSTRUCTION_KIND


def _normalized_flag_key(key: object) -> str:
    return str(key or "").strip().lower().replace("-", "_").replace(" ", "_")


def artifact_challenge_flag_key(
    value: object,
    *,
    declared_keys: frozenset[str] = frozenset(),
    depth: int = 0,
    match_marker_values: bool = True,
) -> str | None:
    """First exact-match anti-bot flag key with a ``True`` value in a run
    artifact, or ``None``. Declared goal-content keys are exempt. With
    ``match_marker_values`` off, only typed boolean flag keys count and enumerated
    string marker values are ignored — used when scanning the run envelope, whose
    string fields are prose/status (``failure_reason`` etc.), not a typed payload."""
    if depth > _MAX_ARTIFACT_FLAG_DEPTH:
        return None
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = _normalized_flag_key(key)
            if normalized in declared_keys:
                continue
            if item is True and normalized in ANTI_BOT_ARTIFACT_FLAG_KEYS:
                return normalized
            if match_marker_values and isinstance(item, str):
                normalized_value = _normalized_flag_key(item)
                if normalized_value in ANTI_BOT_ARTIFACT_MARKER_VALUES:
                    return normalized_value
        for item in value.values():
            found = artifact_challenge_flag_key(
                item, declared_keys=declared_keys, depth=depth + 1, match_marker_values=match_marker_values
            )
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = artifact_challenge_flag_key(
                item, declared_keys=declared_keys, depth=depth + 1, match_marker_values=match_marker_values
            )
            if found:
                return found
    return None
