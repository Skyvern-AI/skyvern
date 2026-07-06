"""Evidence router for ValidationBlock criterion evaluation.

A small classifier that decides whether a validation criterion needs page
evidence (DOM, screenshots, URL) or can be resolved using durable data alone
(criterion text, prior block outputs, workflow inputs).

Conservative by construction:

* The router never accepts the DOM, the screenshots, the current URL, or
  action history as input — page-derived signals would bias classification.
* Only ``DATA_ONLY`` with confidence at or above the configured floor bypasses
  page evidence. Every other result (``MIXED``, ``PAGE_STATE``, low confidence,
  parse error, handler exception, lexical short-circuit) keeps the existing
  page-aware path.
* A lexical short-circuit blocks page-state keywords from ever reaching the
  router, eliminating one cheap LLM call and protecting page-state recall.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Awaitable, Callable

import structlog
from pydantic import BaseModel, Field, ValidationError

from skyvern.forge.prompts import prompt_engine

if TYPE_CHECKING:
    from skyvern.forge.sdk.models import Step

LOG = structlog.get_logger()


class ValidationEvidenceKind(StrEnum):
    """Evidence-source classification for a validation criterion."""

    DATA_ONLY = "data_only"
    PAGE_STATE = "page_state"
    MIXED = "mixed"


class ValidationEvidenceRoute(BaseModel):
    """Strict schema for the router LLM's JSON output.

    ``rationale`` is debug-only and never propagated to user-visible artifacts.
    """

    evidence_kind: ValidationEvidenceKind
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    rationale: str = ""


class ValidationRouterMode(StrEnum):
    """Rollout mode controlled by the ``VALIDATION_EVIDENCE_ROUTER_MODE`` flag."""

    OFF = "off"
    SHADOW = "shadow"
    ENFORCE = "enforce"


class ValidationRouterDecision(StrEnum):
    """Effective decision after applying mode + confidence + lexical guards."""

    PAGE_AWARE = "page_aware"
    DATA_ONLY_NO_PAGE = "data_only_no_page"
    SHADOW_ONLY = "shadow_only"
    FALLBACK = "fallback"


class ValidationRouterFailureReason(StrEnum):
    """Why the router fell back to page-aware. ``None`` means no failure."""

    PARSE_ERROR = "parse_error"
    HANDLER_ERROR = "handler_error"
    LOW_CONFIDENCE = "low_confidence"
    REGEX_SHORT_CIRCUIT = "regex_short_circuit"
    NOT_DATA_ONLY = "not_data_only"


@dataclass
class ValidationRouterResult:
    """Outcome of one router invocation. Carried back to the prompt builder
    so it can choose between the page-aware and no-page rendering paths."""

    effective_without_page_information: bool
    decision: ValidationRouterDecision
    evidence_kind: ValidationEvidenceKind | None = None
    confidence: float | None = None
    failure_reason: ValidationRouterFailureReason | None = None
    rationale: str = ""
    mode: ValidationRouterMode = ValidationRouterMode.OFF


# Positive page-state intent phrases that force PAGE_AWARE without a router
# call. Each phrase represents an explicit intent to evaluate live page/screen
# state. Bare nouns (like "page" or "html") are intentionally excluded — they
# often appear in exclusion clauses ("Do NOT use the page").
#
# The floor is locked by a test — adding more phrases is fine; silently
# dropping any of these would weaken page-state recall.
PAGE_STATE_INTENT_PHRASES: frozenset[str] = frozenset(
    {
        # Subject + action patterns (agent asks "what does the page show?")
        "page shows",
        "page contains",
        "page displays",
        "page has",
        "page indicates",
        "screen shows",
        "screen contains",
        "screen displays",
        "screenshot shows",
        "webpage shows",
        # Visual state words (describes what's visible right now)
        # Bare "shown"/"appears"/"disabled" removed — they cause false
        # positives on data-only criteria ("amount shown in payload").
        "visible",
        "displayed",
        # URL / navigation state
        "url contains",
        "current url",
        "redirected",
        "navigated",
        "navigate to",
        "navigation to",
        # Compound page-state signals (explicit page/UI contexts)
        "button disabled",
        "modal appears",
        "banner visible",
        "error banner",
        "download succeeded",
        "popup appears",
        "file downloaded",
        # Location phrases
        "on the page",
        "on the screen",
        "on the site",
        "from the page",
        # Action + page target phrases
        "check the page",
        "check the screen",
        "confirm the page",
        "verify the page",
        "verify the screen",
        "look at the page",
        "look at the screen",
        "see the page",
        # Interaction phrases
        "click on",
        "click the",
    }
)

# Exclusion-clause patterns — stripped before intent matching so that
# "Do NOT use the page …" does not short-circuit data-only criteria.
#
# Match up to 2 words after the exclusion noun — enough to absorb trailing
# prepositions / list items ("or screenshots", "and HTML elements") without
# swallowing the subsequent page-state clause.
_EXCLUSION_TAIL = r"(?:\s+\S+){0,2}"

_EXCLUSION_PATTERNS: list[re.Pattern[str]] = [
    # "Do NOT use any information from the current webpage, HTML elements,
    #  page tables, or screenshots to make your decision."
    re.compile(
        r"(?:do\s+not|don't|never|should\s+not)\s+"
        r"(?:use|look\s+at|reference|rely\s+on|consider|check|consult)\s+"
        r"(?:any\s+)?(?:information\s+from\s+)?"
        r"(?:the\s+)?(?:current\s+)?"
        r"(?:webpage|html\s+element|page\s+table|page|screenshot|dom|browser|screen)" + _EXCLUSION_TAIL,
        re.IGNORECASE,
    ),
    # "without using page information / html / screenshot / browser"
    re.compile(
        r"without\s+(?:using|looking\s+at|referencing|checking)\s+"
        r"(?:the\s+)?(?:page|html|screenshot|browser|dom|screen|webpage)" + _EXCLUSION_TAIL,
        re.IGNORECASE,
    ),
    # "do not reference the page or screenshots" (shorter form)
    re.compile(
        r"(?:do\s+not|don't)\s+(?:reference|look|check|consult)\s+"
        r"(?:the\s+)?(?:page|screenshot|html|dom|browser|screen|webpage)" + _EXCLUSION_TAIL,
        re.IGNORECASE,
    ),
    # "DATA-ONLY:" / "[DATA-ONLY]" prefix — explicit data-only marker
    re.compile(
        r"\bDATA[-\s]ONLY\b[:\]]?\s*",
        re.IGNORECASE,
    ),
]

# Compiled intent-phrase patterns — each phrase is matched as a whole against
# the cleaned criterion text to avoid the over-matching of bare keywords.
_INTENT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE) for phrase in PAGE_STATE_INTENT_PHRASES
]


def _strip_exclusion_clauses(text: str) -> str:
    """Remove explicit page-evidence exclusion clauses so they do not
    pollute the intent-phrase check with negated mentions."""
    for pattern in _EXCLUSION_PATTERNS:
        text = pattern.sub(" ", text)
    return text


def _has_page_state_intent(text: str) -> bool:
    """Check if the cleaned text contains positive page-state intent."""
    return any(p.search(text) for p in _INTENT_PATTERNS)


def lexical_short_circuit_page_state(criterion: str | None) -> bool:
    """Return True if ``criterion`` expresses positive page/UI-state intent.

    Exclusion clauses (\"Do NOT use the page\", \"without screenshots\",
    \"DATA-ONLY\") are stripped first so they do not trigger a false
    short-circuit.  The remaining text is then checked for intent phrases
    such as \"page shows\", \"URL contains\", \"visible\", \"error banner\",
    and so on.

    A match means we won't even consult the router LLM: the criterion almost
    certainly needs page evidence, and a confused router could otherwise route
    it away from page evidence.
    """
    if not criterion:
        return False
    cleaned = _strip_exclusion_clauses(criterion)
    return _has_page_state_intent(cleaned)


LLMHandler = Callable[..., Awaitable[Any]]


async def route_validation_evidence(
    *,
    complete_criterion: str | None,
    terminate_criterion: str | None,
    navigation_payload_str: str,
    error_code_mapping_str: str | None,
    local_datetime: str,
    mode: ValidationRouterMode,
    min_confidence: float,
    llm_handler: LLMHandler,
    step: Step | None = None,
) -> ValidationRouterResult:
    """Decide whether validation can drop page evidence for this criterion.

    Conservative: any uncertainty maps to page-aware. The router prompt is
    rendered from ``validation-evidence-router.j2`` and is sent to a cheap
    LLM via ``llm_handler``. Screenshots/DOM/URL/action history are never
    forwarded.
    """
    if mode is ValidationRouterMode.OFF:
        return ValidationRouterResult(
            effective_without_page_information=False,
            decision=ValidationRouterDecision.PAGE_AWARE,
            mode=mode,
        )

    # Check criteria AND error code mapping.  error_code_mapping_str is a raw
    # JSON string — scanning it with the lexical guard catches page-state
    # conditions inside error descriptions (e.g. "page displays access denied")
    # that would be missed if the router only looked at criterion text.
    if (
        lexical_short_circuit_page_state(complete_criterion)
        or lexical_short_circuit_page_state(terminate_criterion)
        or lexical_short_circuit_page_state(error_code_mapping_str)
    ):
        return ValidationRouterResult(
            effective_without_page_information=False,
            decision=ValidationRouterDecision.PAGE_AWARE,
            failure_reason=ValidationRouterFailureReason.REGEX_SHORT_CIRCUIT,
            mode=mode,
        )

    try:
        prompt = prompt_engine.load_prompt(
            "validation-evidence-router",
            complete_criterion=complete_criterion,
            terminate_criterion=terminate_criterion,
            navigation_payload_str=navigation_payload_str,
            error_code_mapping_str=error_code_mapping_str,
            local_datetime=local_datetime,
        )
        raw = await llm_handler(
            prompt=prompt,
            prompt_name="validation-evidence-router",
            step=step,
            screenshots=[],
        )
    except Exception as exc:
        LOG.warning(
            "validation-evidence-router handler exception, falling back to page-aware",
            error=str(exc),
        )
        return ValidationRouterResult(
            effective_without_page_information=False,
            decision=ValidationRouterDecision.FALLBACK,
            failure_reason=ValidationRouterFailureReason.HANDLER_ERROR,
            mode=mode,
        )

    try:
        route = ValidationEvidenceRoute.model_validate(raw)
    except ValidationError as exc:
        LOG.warning(
            "validation-evidence-router parse error, falling back to page-aware",
            error=str(exc),
        )
        return ValidationRouterResult(
            effective_without_page_information=False,
            decision=ValidationRouterDecision.FALLBACK,
            failure_reason=ValidationRouterFailureReason.PARSE_ERROR,
            mode=mode,
        )

    confidence = float(route.confidence)
    kind = route.evidence_kind
    rationale = route.rationale or ""

    if mode is ValidationRouterMode.SHADOW:
        return ValidationRouterResult(
            effective_without_page_information=False,
            decision=ValidationRouterDecision.SHADOW_ONLY,
            evidence_kind=kind,
            confidence=confidence,
            rationale=rationale,
            mode=mode,
        )

    if kind is not ValidationEvidenceKind.DATA_ONLY:
        return ValidationRouterResult(
            effective_without_page_information=False,
            decision=ValidationRouterDecision.PAGE_AWARE,
            evidence_kind=kind,
            confidence=confidence,
            failure_reason=ValidationRouterFailureReason.NOT_DATA_ONLY,
            rationale=rationale,
            mode=mode,
        )

    if confidence < min_confidence:
        return ValidationRouterResult(
            effective_without_page_information=False,
            decision=ValidationRouterDecision.PAGE_AWARE,
            evidence_kind=kind,
            confidence=confidence,
            failure_reason=ValidationRouterFailureReason.LOW_CONFIDENCE,
            rationale=rationale,
            mode=mode,
        )

    return ValidationRouterResult(
        effective_without_page_information=True,
        decision=ValidationRouterDecision.DATA_ONLY_NO_PAGE,
        evidence_kind=kind,
        confidence=confidence,
        rationale=rationale,
        mode=mode,
    )
