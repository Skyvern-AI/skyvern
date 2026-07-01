"""Tests for the validation-evidence-router prompt template and the
decisive-criterion-validate template's `without_page_information` branch.

The two templates together implement Route B's contract:

* validation-evidence-router.j2: classifies a criterion. Must not render DOM,
  URL, screenshots, or action history.
* decisive-criterion-validate.j2: when `without_page_information=True`, must
  drop the HTML element block, the page URL line, and any reference to
  screenshots. The page-aware branch must keep behaving the same as on main.
"""

from __future__ import annotations

import json

from skyvern.forge.prompts import prompt_engine

# ---------------------------------------------------------------------------
# validation-evidence-router.j2
# ---------------------------------------------------------------------------


def test_router_prompt_renders_with_minimum_inputs() -> None:
    rendered = prompt_engine.load_prompt(
        "validation-evidence-router",
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str=json.dumps({"invoice_amount": 100}),
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
    )
    assert "extracted_amount equals invoice_amount" in rendered
    assert "invoice_amount" in rendered
    assert "data_only" in rendered
    assert "page_state" in rendered
    assert "mixed" in rendered


def test_router_prompt_excludes_page_evidence_fields() -> None:
    rendered = prompt_engine.load_prompt(
        "validation-evidence-router",
        complete_criterion="some criterion",
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
    )
    lowered = rendered.lower()
    forbidden = [
        "{{ elements",
        "{{ current_url",
        "{{ starting_url",
        "{{ action_history",
        "<html",
        "<body",
        "screenshot",
    ]
    for needle in forbidden:
        assert needle not in lowered, f"router prompt leaked {needle}"


def test_router_prompt_returns_strict_schema_instruction() -> None:
    rendered = prompt_engine.load_prompt(
        "validation-evidence-router",
        complete_criterion="x",
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
    )
    # Make sure the prompt actually asks for the three schema fields.
    assert "evidence_kind" in rendered
    assert "confidence" in rendered
    assert "rationale" in rendered


# ---------------------------------------------------------------------------
# decisive-criterion-validate.j2 — page-aware path (existing main behavior)
# ---------------------------------------------------------------------------


def _render_decisive(without_page_information: bool, *, elements: str = "<dummy/>") -> str:
    return prompt_engine.load_prompt(
        "decisive-criterion-validate",
        elements=elements,
        current_url="https://example.com/path",
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str=json.dumps({"invoice_amount": 100}),
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
        without_page_information=without_page_information,
    )


def test_decisive_page_aware_renders_elements_and_url() -> None:
    rendered = _render_decisive(without_page_information=False, elements="<form data-id='abc'/>")
    assert "<form data-id='abc'/>" in rendered
    assert "https://example.com/path" in rendered
    assert "HTML elements" in rendered


def test_decisive_page_aware_default_behavior_unchanged_when_flag_absent() -> None:
    """Callers that don't pass `without_page_information` must see the original
    behavior. Backward compatibility: the field is optional and defaults to
    page-aware."""
    rendered = prompt_engine.load_prompt(
        "decisive-criterion-validate",
        elements="<form data-id='abc'/>",
        current_url="https://example.com/path",
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_payload_str=json.dumps({"invoice_amount": 100}),
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
    )
    assert "<form data-id='abc'/>" in rendered
    assert "https://example.com/path" in rendered


# ---------------------------------------------------------------------------
# decisive-criterion-validate.j2 — no-page path (Route B)
# ---------------------------------------------------------------------------


def test_decisive_without_page_drops_html_elements() -> None:
    rendered = _render_decisive(without_page_information=True, elements="<form data-id='abc'/>")
    assert "<form data-id='abc'/>" not in rendered
    assert "HTML elements" not in rendered


def test_decisive_without_page_drops_current_url_line() -> None:
    rendered = _render_decisive(without_page_information=True, elements="<form/>")
    assert "https://example.com/path" not in rendered


def test_decisive_without_page_drops_screenshot_reference() -> None:
    rendered = _render_decisive(without_page_information=True, elements="<form/>")
    # Asking the LLM to use screenshots when none are attached confuses it and
    # invites it to hallucinate visual evidence. The no-page branch must not
    # mention screenshots at all.
    assert "screenshot" not in rendered.lower()


def test_decisive_without_page_preserves_criterion_and_user_details() -> None:
    rendered = _render_decisive(without_page_information=True, elements="<form/>")
    assert "extracted_amount equals invoice_amount" in rendered
    assert "invoice_amount" in rendered
