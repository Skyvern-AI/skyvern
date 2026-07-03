"""Tests for the validation evidence router.

The router classifies a ValidationBlock criterion as `data_only`, `page_state`,
or `mixed` using a cheap LLM call whose input excludes DOM / URL / screenshots /
action history. Only high-confidence `data_only` routes bypass page evidence;
every other outcome (mixed, page_state, low confidence, parse error, handler
error, lexical short-circuit match) falls back to the existing page-aware path.

The page-state recall guarantee is the asymmetric constraint we trade against:
it is far better to keep a data-only criterion on the page-aware path than to
route a page-state criterion away from page evidence.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.validation_evidence_router import (
    PAGE_STATE_INTENT_PHRASES,
    ValidationEvidenceKind,
    ValidationEvidenceRoute,
    ValidationRouterDecision,
    ValidationRouterFailureReason,
    ValidationRouterMode,
    ValidationRouterResult,
    lexical_short_circuit_page_state,
    route_validation_evidence,
)


def _ok_handler(**route: Any) -> AsyncMock:
    """Build a fake LLM handler returning a valid router JSON payload."""
    handler = AsyncMock(return_value=route)
    return handler


def _payload(**route: Any) -> dict[str, Any]:
    base = {"evidence_kind": "data_only", "confidence": 0.95, "rationale": "stub"}
    base.update(route)
    return base


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_route_schema_accepts_all_three_kinds() -> None:
    for kind in ("data_only", "page_state", "mixed"):
        route = ValidationEvidenceRoute(evidence_kind=kind, confidence=0.5, rationale="ok")
        assert route.evidence_kind.value == kind


def test_route_schema_rejects_unknown_kind() -> None:
    with pytest.raises(Exception):
        ValidationEvidenceRoute(evidence_kind="unknown", confidence=0.5, rationale="ok")


def test_route_schema_rejects_out_of_range_confidence() -> None:
    with pytest.raises(Exception):
        ValidationEvidenceRoute(evidence_kind="data_only", confidence=1.5, rationale="ok")
    with pytest.raises(Exception):
        ValidationEvidenceRoute(evidence_kind="data_only", confidence=-0.1, rationale="ok")


# ---------------------------------------------------------------------------
# Lexical short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "criterion",
    [
        "The page shows a confirmation message.",
        "The URL contains /success.",
        "A modal appears with the success message.",
        "The submit button is disabled on the page.",
        "An error banner is visible at the top.",
        "Navigate to the next screen and check.",
        "The element with id 'foo' is displayed on the screen.",
        "The screenshot shows the right page.",
        "After clicking, the page is displayed.",
        "Download the receipt from the page.",
        "The link `Sign up` is shown on the page.",
    ],
)
def test_lexical_short_circuit_matches_page_keywords(criterion: str) -> None:
    assert lexical_short_circuit_page_state(criterion) is True


@pytest.mark.parametrize(
    "criterion",
    [
        "extracted_amount equals invoice_amount",
        "The list `accounts` has at least 3 entries",
        "user_details.first_name is not empty",
        "today's date is after the deadline",
        None,
        "",
    ],
)
def test_lexical_short_circuit_passes_data_only_phrases(criterion: str | None) -> None:
    assert lexical_short_circuit_page_state(criterion) is False


def test_page_state_intent_phrases_cover_required_patterns() -> None:
    """The hybrid lexical guard uses positive page-state intent phrases instead
    of bare keywords. Adding a phrase is fine; dropping one would weaken
    page-state recall, so we lock the floor here."""
    required = {
        "page shows",
        "page contains",
        "screen displays",
        "visible",
        "displayed",
        "url contains",
        "current url",
        "redirected",
        "navigated",
        "button disabled",
        "modal appears",
        "banner visible",
        "error banner",
        "download succeeded",
        "on the page",
        "check the page",
        "navigate to",
        "popup appears",
    }
    assert required.issubset(PAGE_STATE_INTENT_PHRASES)


# ---------------------------------------------------------------------------
# Mode gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mode_off_skips_router_and_returns_page_aware() -> None:
    handler = AsyncMock()
    result = await route_validation_evidence(
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
        mode=ValidationRouterMode.OFF,
        min_confidence=0.9,
        llm_handler=handler,
    )
    assert isinstance(result, ValidationRouterResult)
    assert result.effective_without_page_information is False
    assert result.decision is ValidationRouterDecision.PAGE_AWARE
    assert result.failure_reason is None
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_mode_shadow_calls_router_but_stays_page_aware() -> None:
    handler = AsyncMock(return_value=_payload(evidence_kind="data_only", confidence=0.99))
    result = await route_validation_evidence(
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
        mode=ValidationRouterMode.SHADOW,
        min_confidence=0.9,
        llm_handler=handler,
    )
    assert result.effective_without_page_information is False
    assert result.decision is ValidationRouterDecision.SHADOW_ONLY
    assert result.evidence_kind is ValidationEvidenceKind.DATA_ONLY
    assert result.confidence == pytest.approx(0.99)
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_mode_enforce_high_confidence_data_only_routes_without_page() -> None:
    handler = AsyncMock(return_value=_payload(evidence_kind="data_only", confidence=0.97))
    result = await route_validation_evidence(
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
        mode=ValidationRouterMode.ENFORCE,
        min_confidence=0.9,
        llm_handler=handler,
    )
    assert result.effective_without_page_information is True
    assert result.decision is ValidationRouterDecision.DATA_ONLY_NO_PAGE
    assert result.failure_reason is None


# ---------------------------------------------------------------------------
# Conservative fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enforce_page_state_kind_stays_page_aware() -> None:
    handler = AsyncMock(return_value=_payload(evidence_kind="page_state", confidence=0.99))
    result = await route_validation_evidence(
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
        mode=ValidationRouterMode.ENFORCE,
        min_confidence=0.9,
        llm_handler=handler,
    )
    assert result.effective_without_page_information is False
    assert result.decision is ValidationRouterDecision.PAGE_AWARE
    assert result.failure_reason is ValidationRouterFailureReason.NOT_DATA_ONLY


@pytest.mark.asyncio
async def test_enforce_mixed_kind_stays_page_aware() -> None:
    handler = AsyncMock(return_value=_payload(evidence_kind="mixed", confidence=0.95))
    result = await route_validation_evidence(
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
        mode=ValidationRouterMode.ENFORCE,
        min_confidence=0.9,
        llm_handler=handler,
    )
    assert result.effective_without_page_information is False
    assert result.decision is ValidationRouterDecision.PAGE_AWARE
    assert result.failure_reason is ValidationRouterFailureReason.NOT_DATA_ONLY


@pytest.mark.asyncio
async def test_enforce_low_confidence_data_only_stays_page_aware() -> None:
    handler = AsyncMock(return_value=_payload(evidence_kind="data_only", confidence=0.7))
    result = await route_validation_evidence(
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
        mode=ValidationRouterMode.ENFORCE,
        min_confidence=0.9,
        llm_handler=handler,
    )
    assert result.effective_without_page_information is False
    assert result.decision is ValidationRouterDecision.PAGE_AWARE
    assert result.failure_reason is ValidationRouterFailureReason.LOW_CONFIDENCE


@pytest.mark.asyncio
async def test_enforce_parse_error_stays_page_aware() -> None:
    handler = AsyncMock(return_value={"evidence_kind": "data_only"})  # missing confidence
    result = await route_validation_evidence(
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
        mode=ValidationRouterMode.ENFORCE,
        min_confidence=0.9,
        llm_handler=handler,
    )
    assert result.effective_without_page_information is False
    assert result.decision is ValidationRouterDecision.FALLBACK
    assert result.failure_reason is ValidationRouterFailureReason.PARSE_ERROR


@pytest.mark.asyncio
async def test_enforce_unknown_kind_stays_page_aware() -> None:
    handler = AsyncMock(return_value={"evidence_kind": "definitely_data", "confidence": 0.99, "rationale": "x"})
    result = await route_validation_evidence(
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
        mode=ValidationRouterMode.ENFORCE,
        min_confidence=0.9,
        llm_handler=handler,
    )
    assert result.effective_without_page_information is False
    assert result.decision is ValidationRouterDecision.FALLBACK
    assert result.failure_reason is ValidationRouterFailureReason.PARSE_ERROR


@pytest.mark.asyncio
async def test_enforce_prompt_render_exception_stays_page_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the router prompt itself fails to render (template removed, jinja
    error, etc.), the router must conservatively fall back to page-aware
    instead of letting the exception bubble up and break prompt construction."""
    handler = AsyncMock(return_value=_payload(evidence_kind="data_only", confidence=0.99))

    def explode(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("template render boom")

    from skyvern.forge import validation_evidence_router as ver

    monkeypatch.setattr(ver.prompt_engine, "load_prompt", explode)
    result = await route_validation_evidence(
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
        mode=ValidationRouterMode.ENFORCE,
        min_confidence=0.9,
        llm_handler=handler,
    )
    assert result.effective_without_page_information is False
    assert result.decision is ValidationRouterDecision.FALLBACK
    assert result.failure_reason is ValidationRouterFailureReason.HANDLER_ERROR
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_enforce_handler_exception_stays_page_aware() -> None:
    handler = AsyncMock(side_effect=RuntimeError("boom"))
    result = await route_validation_evidence(
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
        mode=ValidationRouterMode.ENFORCE,
        min_confidence=0.9,
        llm_handler=handler,
    )
    assert result.effective_without_page_information is False
    assert result.decision is ValidationRouterDecision.FALLBACK
    assert result.failure_reason is ValidationRouterFailureReason.HANDLER_ERROR


@pytest.mark.asyncio
async def test_enforce_lexical_short_circuit_skips_llm_and_stays_page_aware() -> None:
    """Even with a `data_only`-looking criterion in another slot, a single
    page-keyword match must short-circuit. The router LLM is not called."""
    handler = AsyncMock()
    result = await route_validation_evidence(
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion="If the page shows an error banner, terminate.",
        navigation_payload_str="{}",
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
        mode=ValidationRouterMode.ENFORCE,
        min_confidence=0.9,
        llm_handler=handler,
    )
    assert result.effective_without_page_information is False
    assert result.decision is ValidationRouterDecision.PAGE_AWARE
    assert result.failure_reason is ValidationRouterFailureReason.REGEX_SHORT_CIRCUIT
    handler.assert_not_awaited()


# ---------------------------------------------------------------------------
# Router prompt input must not leak page evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_llm_prompt_excludes_page_evidence_fields() -> None:
    """The router's whole purpose is to classify by intent, not by page state.
    Any DOM/URL/screenshot/action history leakage into the prompt would bias
    the classifier toward PAGE_STATE and defeat data-only routing."""
    captured: dict[str, Any] = {}

    async def fake_handler(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _payload(evidence_kind="data_only", confidence=0.97)

    await route_validation_evidence(
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str=json.dumps({"first_name": "Ada"}),
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
        mode=ValidationRouterMode.ENFORCE,
        min_confidence=0.9,
        llm_handler=fake_handler,
    )

    assert captured.get("screenshots") in (None, [])
    prompt = captured.get("prompt") or ""
    banned_substrings = [
        "<html",
        "<body",
        "current_url",
        "starting_url",
        "action history",
        "actions_and_results",
        "data-skyvern",
        "skyvern-id",
        "open_tabs",
        "recent_dialog",
        "<iframe",
    ]
    lowered = prompt.lower()
    for needle in banned_substrings:
        assert needle.lower() not in lowered, f"router prompt leaked '{needle}'"


@pytest.mark.asyncio
async def test_router_llm_prompt_includes_criterion_and_user_details() -> None:
    captured: dict[str, Any] = {}

    async def fake_handler(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _payload(evidence_kind="data_only", confidence=0.97)

    await route_validation_evidence(
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str=json.dumps({"invoice_amount": 100, "extracted_amount": 100}),
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
        mode=ValidationRouterMode.ENFORCE,
        min_confidence=0.9,
        llm_handler=fake_handler,
    )

    prompt = captured.get("prompt") or ""
    assert "extracted_amount equals invoice_amount" in prompt
    assert "invoice_amount" in prompt
    assert "extracted_amount" in prompt


# ---------------------------------------------------------------------------
# Eval-style table (page-state recall is the hard gate)
# ---------------------------------------------------------------------------

# Inputs where the *expected router classification* in synthetic ideal world is
# noted, but the binding contract we assert is: every PAGE_STATE / MIXED case
# must end up `effective_without_page_information=False`. Data-only routing
# being "missed" (PAGE_AWARE) is acceptable; routing page-state evidence away
# is not.
EVAL_CASES = [
    # (criterion, simulated router output, expected_effective_no_page)
    # --- page-state hard gates ---
    (
        "The page shows a thank-you confirmation message.",
        # Even if a confused router said data_only, the lexical guard catches it.
        ("data_only", 0.99),
        False,
    ),
    (
        "The URL contains `/success` after submission.",
        ("data_only", 0.95),
        False,
    ),
    (
        "An error banner appears at the top of the page.",
        ("data_only", 0.95),
        False,
    ),
    (
        "The Submit button becomes disabled on the page.",
        ("data_only", 0.95),
        False,
    ),
    # --- mixed: still page-aware ---
    (
        "extracted_amount matches invoice and confirmation message visible",
        ("mixed", 0.98),
        False,
    ),
    # --- data-only: routed away from page ---
    (
        "extracted_amount equals invoice_amount",
        ("data_only", 0.95),
        True,
    ),
    (
        "today's date is on or before the deadline",
        ("data_only", 0.93),
        True,
    ),
    (
        "user_details.email is a valid address",
        ("data_only", 0.92),
        True,
    ),
    # --- data-only but low confidence: page-aware ---
    (
        "user_details.email is a valid address",
        ("data_only", 0.7),
        False,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("criterion,router_output,expected_no_page", EVAL_CASES)
async def test_eval_cases_preserve_page_state_recall(
    criterion: str, router_output: tuple[str, float], expected_no_page: bool
) -> None:
    kind, confidence = router_output
    handler = AsyncMock(return_value=_payload(evidence_kind=kind, confidence=confidence))
    result = await route_validation_evidence(
        complete_criterion=criterion,
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
        mode=ValidationRouterMode.ENFORCE,
        min_confidence=0.9,
        llm_handler=handler,
    )
    assert result.effective_without_page_information is expected_no_page


@pytest.mark.asyncio
async def test_eval_page_state_recall_is_one_hundred_percent() -> None:
    """Asymmetric guarantee: across every PAGE_STATE / MIXED labelled case
    above, the router must NEVER drop page evidence. This is the SKY-10593
    safety floor."""
    failures: list[str] = []
    for criterion, router_output, _expected in EVAL_CASES:
        kind, confidence = router_output
        # Worst-case adversarial: router insists data_only at max confidence.
        adversarial_kind = "data_only"
        adversarial_conf = 0.99
        if lexical_short_circuit_page_state(criterion):
            label = "page_state"
        else:
            # No lexical hit → trust the labelled kind for the recall check.
            label = kind
        handler = AsyncMock(return_value=_payload(evidence_kind=adversarial_kind, confidence=adversarial_conf))
        result = await route_validation_evidence(
            complete_criterion=criterion,
            terminate_criterion=None,
            navigation_payload_str="{}",
            error_code_mapping_str=None,
            local_datetime="2026-06-08T00:00:00",
            mode=ValidationRouterMode.ENFORCE,
            min_confidence=0.9,
            llm_handler=handler,
        )
        if label in ("page_state", "mixed") and result.effective_without_page_information:
            failures.append(criterion)
    assert failures == [], f"page-state recall regression: {failures}"


# ---------------------------------------------------------------------------
# Regression test for a data-only validation failure pattern.
# The validation block falsely terminated because the LLM anchored on page
# DOM despite the criterion explicitly saying DATA-ONLY and providing inline
# values from prior block output (e.g., OCR / file_url_parser).
# ---------------------------------------------------------------------------

ARTIFACT_COMPLETE_CRITERION = (
    "DATA-ONLY VALIDATION - evaluate ONLY the values from the OCR output below. "
    "Do NOT use any information from the current webpage, HTML elements, page tables, "
    "or screenshots to make your decision."
    "\n\n"
    "get the billing date and the account number extracted from:"
    "{'account_number_extracted': '123 456 7890 12', 'invoice_date_extracted': '2026-06-18'}"
    "\n\n"
    "if it is between the 2026-06-11 and 2026-06-25, and the account_number_extracted "
    "is the same as 123456789012 or 123456789012 ignoring formatting differences "
    "(like slashes, dashes or trailing/additional digits). then complete"
)

ARTIFACT_TERMINATE_CRITERION = (
    "if it is not between the 2026-06-11 and 2026-06-25 or the account is incorrect then terminate"
)

ARTIFACT_ERROR_CODE_MAPPING = {
    "incorrect_file_downloaded": (
        "the bill date is not between 2026-06-11 and 2026-06-25 or the account number is incorrect, terminate"
    )
}


def test_artifact_criterion_passes_lexical_short_circuit() -> None:
    """After the hybrid lexical guard fix, the production artifact criterion must
    NOT trigger the short-circuit because its page references appear only in an
    explicit exclusion clause (\"Do NOT use... webpage, HTML elements, page
    tables, or screenshots\") that the pre-normalizer strips."""
    assert lexical_short_circuit_page_state(ARTIFACT_COMPLETE_CRITERION) is False
    # The terminate criterion is a simple data comparison, no page keywords.
    assert lexical_short_circuit_page_state(ARTIFACT_TERMINATE_CRITERION) is False


@pytest.mark.asyncio
async def test_artifact_criterion_routes_data_only() -> None:
    """With the hybrid lexical guard, the production artifact criterion bypasses
    the short-circuit and reaches the router. When the router returns
    high-confidence data_only, the route must be DATA_ONLY_NO_PAGE."""
    handler = AsyncMock(return_value=_payload(evidence_kind="data_only", confidence=0.95))
    result = await route_validation_evidence(
        complete_criterion=ARTIFACT_COMPLETE_CRITERION,
        terminate_criterion=ARTIFACT_TERMINATE_CRITERION,
        navigation_payload_str="{}",
        error_code_mapping_str=json.dumps(ARTIFACT_ERROR_CODE_MAPPING),
        local_datetime="2026-06-30T04:12:48",
        mode=ValidationRouterMode.ENFORCE,
        min_confidence=0.9,
        llm_handler=handler,
    )
    assert result.effective_without_page_information is True
    assert result.decision is ValidationRouterDecision.DATA_ONLY_NO_PAGE
    assert result.evidence_kind is ValidationEvidenceKind.DATA_ONLY
    assert result.confidence == 0.95
    handler.assert_called_once()


# ---------------------------------------------------------------------------
# Hybrid lexical guard counterexamples
# Exclusion clauses are stripped; positive page-state intent still short-circuits.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "criterion",
    [
        # Exclusion clause followed by positive page-state intent
        "Do NOT use screenshots. If the page shows an error banner, terminate.",
        "Do not look at the HTML. The URL contains /success.",
        # Exclusion clause + same-sentence page-state intent (semicolon / comma)
        "Do NOT use screenshots; if the page shows an error banner, terminate.",
        "without using page information, if the URL contains /success then complete",
        "do not reference the page, but verify the screen shows confirmation",
        # Exclusion clause + same-sentence page-state intent (no delimiter
        # before conjunction — "but" / "if" signals clause boundary)
        "Do NOT use screenshots but verify the screen shows confirmation",
        "Do NOT use screenshots if the page shows an error banner, terminate",
        "without using page information but the URL contains /success",
        # Positive page-state intent without exclusion
        "If no error banner is visible, complete.",
        "The URL contains /success.",
        "The page shows a confirmation message.",
        "The screen displays a thank-you message.",
        "An error banner is visible at the top.",
        "The submit button appears disabled on the page.",
        "If the modal appears, terminate.",
        "The download succeeded.",
        "If you are redirected to the dashboard, complete.",
        # Exclusion phrase followed by page-state intent (no punctuation boundary)
        "without checking the page confirm the URL contains X and balance is visible",
        "without checking the page check the balance on the screen",
        "without checking the page ensure the balance is visible",
        # Check/confirm/verify page
        "check the page for an error message",
        "verify the screen shows the confirmation",
        # on the page / on the screen
        "the text on the page says complete",
        "look for the success message on the screen",
    ],
)
def test_hybrid_lexical_short_circuits_page_state_intent(criterion: str) -> None:
    assert lexical_short_circuit_page_state(criterion) is True


@pytest.mark.parametrize(
    "criterion",
    [
        # Exclusion clauses with NO positive page-state intent after stripping
        "Do NOT use any information from the current webpage, HTML elements, "
        "page tables, or screenshots to make your decision. "
        "extracted_amount equals invoice_amount",
        "without using page information, evaluate the data",
        "DATA-ONLY: do not reference the page or screenshots, just compare the values",
        "do not look at the browser, only check the extracted fields",
        # Pure data comparisons (no page references at all)
        "extracted_amount equals invoice_amount",
        "The list `accounts` has at least 3 entries",
        "user_details.first_name is not empty",
        "today's date is after the deadline",
        "account_number_extracted matches 123456789012 ignoring formatting",
        "invoice_date_extracted is between 2026-06-11 and 2026-06-25",
        # Data-only phrases that formerly triggered false positives via bare
        # "disabled" / "shown" / "look for" — now correctly pass through.
        "the feature is disabled in the config",
        "the amount shown in the payload equals total",
        "look for an error code in the response",
        None,
        "",
    ],
)
def test_hybrid_lexical_passes_exclusion_only_and_data_only(criterion: str | None) -> None:
    assert lexical_short_circuit_page_state(criterion) is False


# ---------------------------------------------------------------------------
# Error code mapping lexical guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_state_error_code_mapping_forces_page_aware() -> None:
    """Complete/terminate criteria are data-only, but error_code_mapping contains
    page-state intent (e.g. page displays access denied). The lexical guard must
    check error_code_mapping_str too and short-circuit to PAGE_AWARE."""
    handler = AsyncMock()
    result = await route_validation_evidence(
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str='{"access_denied": "page displays access denied banner"}',
        local_datetime="2026-06-08T00:00:00",
        mode=ValidationRouterMode.ENFORCE,
        min_confidence=0.9,
        llm_handler=handler,
    )
    assert result.effective_without_page_information is False
    assert result.decision is ValidationRouterDecision.PAGE_AWARE
    assert result.failure_reason is ValidationRouterFailureReason.REGEX_SHORT_CIRCUIT
    handler.assert_not_called()


@pytest.mark.asyncio
async def test_data_only_error_code_mapping_allows_router_call() -> None:
    """Error_code_mapping contains data-only conditions (no page-state intent).
    The router should be called normally since neither criteria nor error
    mapping triggers the lexical guard."""
    handler = AsyncMock(return_value=_payload(evidence_kind="data_only", confidence=0.95))
    result = await route_validation_evidence(
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str='{"bad_account": "account number does not match extracted value"}',
        local_datetime="2026-06-08T00:00:00",
        mode=ValidationRouterMode.ENFORCE,
        min_confidence=0.9,
        llm_handler=handler,
    )
    assert result.effective_without_page_information is True
    assert result.decision is ValidationRouterDecision.DATA_ONLY_NO_PAGE
    handler.assert_called_once()
