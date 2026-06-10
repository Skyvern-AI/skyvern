"""Tests for evidence-grounded Copilot composition."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.build_phase import BuildPhase
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    COMPOSITION_STRUCTURED_EVIDENCE_EXPRESSION,
    COMPOSITION_VISUAL_OBSTRUCTION_CANDIDATES_EXPRESSION,
)
from skyvern.forge.sdk.copilot.composition_evidence import (
    composition_page_evidence_error,
    has_bounded_page_schema,
    merge_visual_composition_evidence,
    normalize_block_observation_refs,
    page_evidence_needs_visual_fallback,
    parse_composition_html,
    parse_composition_structured,
)
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentMode
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence


@dataclass
class _Ctx:
    build_phase: BuildPhase = BuildPhase.COMPOSING
    turn_intent: TurnIntent = field(default_factory=lambda: TurnIntent(mode=TurnIntentMode.BUILD))
    composition_page_evidence: dict | None = None
    workflow_yaml: str | None = None
    flow_evidence: list[dict] = field(default_factory=list)
    # Looser than AgentContext so tests can feed malformed refs into the gate.
    block_observation_refs: dict[str, object] = field(default_factory=dict)
    raw_block_observation_refs: object | None = None
    prior_observed_acted_pages: list[dict] = field(default_factory=list)
    per_tool_budget_problem_block_labels: list[str] = field(default_factory=list)
    workflow_verification_evidence: WorkflowVerificationEvidence = field(default_factory=WorkflowVerificationEvidence)
    post_run_page_observation_after_failed_test: bool = False
    last_failure_category_top: str | None = None


def _flow_entry(
    url: str,
    *,
    reached_via: str = "navigate",
    with_form: bool = True,
    observed_empty_page: bool = False,
    step: int = 0,
) -> dict:
    evidence: dict = {
        "inspected_url": url,
        "current_url": url,
        "source_tool": "inspect_page_for_composition",
        "forms": [{"fields": [_field("X", "x")], "submit_controls": []}] if with_form else [],
        "navigation_targets": [],
        "result_containers": [],
        "challenge_controls": [],
        "observed_empty_page": observed_empty_page,
    }
    return {
        "evidence": evidence,
        "reached_via": reached_via,
        "had_bounded_schema": with_form or observed_empty_page,
        "step": step,
    }


def _scout_interaction_entry(url: str, *, step: int, selector: str = "#x") -> dict:
    # A scout-interaction observation carries the proven selector but no page schema.
    return {
        "evidence": {
            "inspected_url": url,
            "current_url": url,
            "source_tool": "scout_interaction",
            "interaction_tool": "click",
            "interaction_selector": selector,
        },
        "reached_via": "interaction",
        "had_bounded_schema": False,
        "step": step,
    }


def _yaml(*blocks: dict) -> str:
    return yaml.safe_dump({"title": "wf", "workflow_definition": {"parameters": [], "blocks": list(blocks)}})


def _field(label: str, name: str) -> dict:
    return {"name": name, "id": name, "label": label, "type": "text", "placeholder": "", "selector": f"#{name}"}


def _first_last_evidence() -> dict:
    return {
        "inspected_url": "https://example.com/lookup",
        "current_url": "https://example.com/lookup",
        "source_tool": "inspect_page_for_composition",
        "forms": [
            {
                "fields": [_field("First Name", "firstName"), _field("Last Name", "lastName")],
                "submit_controls": [{"text": "Search", "id": "searchButton", "selector": "#searchButton"}],
            }
        ],
    }


def test_composition_parse_html_extracts_labeled_fields_and_submit_controls() -> None:
    parsed = parse_composition_html(
        """
        <html><head><title>Lookup</title></head><body>
          <h1>Credential lookup</h1>
          <form id="searchForm" action="/results">
            <label for="firstName">First Name</label>
            <input id="firstName" name="firstName" type="text" required />
            <label for="lastName">Last Name</label>
            <input id="lastName" name="lastName" type="text" />
            <button id="searchButton" type="submit">Search</button>
          </form>
          <a id="openLookup" href="/registry/search">Find a Record</a>
          <a id="external" href="https://evil.example/steal">External</a>
          <table id="results"><tbody></tbody></table>
        </body></html>
        """,
        inspected_url="https://example.com/lookup",
        current_url="https://example.com/lookup",
    )

    assert parsed["page_title"] == "Lookup Credential lookup"
    assert parsed["forms"][0]["fields"][0]["name"] == "firstName"
    assert parsed["forms"][0]["fields"][0]["label"] == "First Name"
    assert parsed["forms"][0]["fields"][0]["required"] is True
    assert parsed["forms"][0]["fields"][1]["name"] == "lastName"
    assert parsed["forms"][0]["submit_controls"][0]["text"] == "Search"
    assert parsed["navigation_targets"][0]["text"] == "Find a Record"
    assert parsed["navigation_targets"][0]["href"] == "https://example.com/registry/search"
    assert [item["text"] for item in parsed["navigation_targets"]] == ["Find a Record"]
    assert parsed["result_containers"][0]["selector"] == "#results"
    assert parsed["result_containers"][0]["row_selector"] == "#results tbody tr"
    assert "#results tbody tr td:first-child" in parsed["result_containers"][0]["expand_toggle_candidates"]
    assert parsed["evidence_sources"] == ["dom_html"]
    assert parsed["screenshot_used"] is False
    assert parsed["visual_evidence_summary"] == ""
    assert parsed["challenge_state"]["detected"] is False
    assert parsed["source_tool"] == "inspect_page_for_composition"


def test_composition_parse_html_extracts_modal_overlay_controls() -> None:
    parsed = parse_composition_html(
        """
        <html><body>
          <div id="newsletter" role="dialog" aria-modal="true" class="promo-modal">
            <h2>Get updates</h2>
            <p>Join our list before browsing.</p>
            <button aria-label="Close modal">x</button>
            <button>No thanks</button>
          </div>
        </body></html>
        """,
        inspected_url="https://example.com/results",
        current_url="https://example.com/results",
    )

    assert parsed["modal_overlays"][0]["selector"] == "#newsletter"
    assert parsed["modal_overlays"][0]["role"] == "dialog"
    assert parsed["modal_overlays"][0]["dismiss_controls"][0]["text"] == "x"
    assert parsed["modal_overlays"][0]["dismiss_controls"][0]["aria_label"] == "Close modal"
    assert parsed["page_obstructions"][0]["kind"] == "modal_overlay"
    assert parsed["page_obstructions"][0]["visible_controls"][0]["text"] == "x"
    assert has_bounded_page_schema(parsed) is True


def test_composition_parse_html_extracts_class_only_modal_overlay() -> None:
    parsed = parse_composition_html(
        """
        <html><body>
          <div class="promo modal">
            <h2>Before you continue</h2>
            <button>Close</button>
          </div>
        </body></html>
        """,
        inspected_url="https://example.com/results",
        current_url="https://example.com/results",
    )

    assert parsed["modal_overlays"][0]["class"] == "promo modal"
    assert parsed["modal_overlays"][0]["dismiss_controls"][0]["text"] == "Close"
    assert parsed["page_obstructions"][0]["visible_controls"][0]["text"] == "Close"
    assert has_bounded_page_schema(parsed) is True


def test_composition_parse_html_ignores_hidden_modal_overlay_markup() -> None:
    parsed = parse_composition_html(
        """
        <html><body>
          <div id="closedDialog" role="dialog" aria-modal="true" aria-hidden="true">
            <button>Close</button>
          </div>
          <div id="closedModal" class="modal" style="display: none;">
            <button>Dismiss</button>
          </div>
          <div aria-hidden="true">
            <div id="wrappedDialog" role="dialog">
              <button>Close</button>
            </div>
          </div>
        </body></html>
        """,
        inspected_url="https://example.com/results",
        current_url="https://example.com/results",
    )

    assert parsed["modal_overlays"] == []
    assert parsed["page_obstructions"] == []


def test_composition_parse_html_marks_generic_fullscreen_barrier_for_visual_fallback() -> None:
    parsed = parse_composition_html(
        """
        <html><body>
          <form id="search">
            <input id="query" name="query" type="text" />
            <button>Search</button>
          </form>
          <section
            id="interruption"
            style="position: fixed; inset: 0; z-index: 1200; background: rgba(0,0,0,.35);"
          >
            <article>
              <p>Finish this checkpoint before continuing.</p>
              <button>Continue</button>
            </article>
          </section>
        </body></html>
        """,
        inspected_url="https://example.com/results",
        current_url="https://example.com/results",
    )

    assert parsed["modal_overlays"] == []
    assert parsed["page_obstructions"] == []
    assert parsed["visual_obstruction_candidates"] == [
        {
            "source": "dom_style",
            "position": "fixed",
            "coverage": "viewport",
            "has_visible_controls": True,
        }
    ]
    assert has_bounded_page_schema(parsed) is True
    assert page_evidence_needs_visual_fallback(parsed) is True

    merged = merge_visual_composition_evidence(
        parsed,
        visual_summary={
            "summary": "A centered checkpoint panel blocks the search form.",
            "challenge_detected": False,
            "submit_blocked": False,
            "page_obstruction_detected": True,
            "obstruction_kind": "checkpoint_panel",
            "obstruction_location": "Centered over the search form.",
            "underlying_page_blocked": True,
            "visible_dismiss_controls": ["Continue"],
        },
    )

    assert merged["page_obstructions"] == [
        {
            "kind": "checkpoint_panel",
            "source": "vision_summary",
            "visual_location": "Centered over the search form.",
            "visible_controls": [{"text": "Continue"}],
            "underlying_page_blocked": True,
        }
    ]


def test_composition_parse_html_does_not_screenshot_normal_fixed_footer() -> None:
    parsed = parse_composition_html(
        """
        <html><body>
          <form id="search">
            <input id="query" name="query" type="text" />
            <button>Search</button>
          </form>
          <footer style="position: fixed; bottom: 0; left: 0; right: 0; z-index: 1200;">
            <button>Accept</button>
          </footer>
        </body></html>
        """,
        inspected_url="https://example.com/results",
        current_url="https://example.com/results",
    )

    assert parsed["visual_obstruction_candidates"] == []
    assert has_bounded_page_schema(parsed) is True
    assert page_evidence_needs_visual_fallback(parsed) is False


def test_composition_parse_html_ignores_empty_modal_root_as_bounded_schema() -> None:
    parsed = parse_composition_html(
        """
        <html><body>
          <div id="modal-root"></div>
        </body></html>
        """,
        inspected_url="https://example.com/results",
        current_url="https://example.com/results",
    )

    assert parsed["modal_overlays"] == []
    assert has_bounded_page_schema(parsed) is False


def test_composition_parse_html_does_not_treat_next_as_modal_dismiss_control() -> None:
    parsed = parse_composition_html(
        """
        <html><body>
          <div id="modal-root">
            <button>Next</button>
            <button>Export</button>
          </div>
        </body></html>
        """,
        inspected_url="https://example.com/results",
        current_url="https://example.com/results",
    )

    assert parsed["modal_overlays"] == []
    assert has_bounded_page_schema(parsed) is False


def test_composition_parse_html_preserves_stable_control_selectors_and_values() -> None:
    parsed = parse_composition_html(
        """
        <html><body>
          <form id="registrySearch">
            <input class="credentialTypeChoice" type="checkbox" value="STANDARD" /> Standard
            <input id="id-first_name" name="first_name" type="text" />
            <input id="id-last_name" name="last_name" type="text" />
            <select id="state" name="state">
              <option value="">Any State</option>
              <option value="MA">Massachusetts</option>
            </select>
            <input class="acknowledgementCheck" type="checkbox" value="yes" /> I agree
            <input id="btnSubmit" type="button" value="Search" />
          </form>
        </body></html>
        """,
        inspected_url="https://example.com/search",
        current_url="https://example.com/search",
    )

    fields = parsed["forms"][0]["fields"]
    assert fields[0]["selector"] == 'input.credentialTypeChoice[value="STANDARD"]'
    assert fields[0]["label"] == "Standard"
    assert fields[0]["value"] == "STANDARD"
    assert fields[1]["selector"] == "#id-first_name"
    assert fields[2]["selector"] == "#id-last_name"
    assert fields[3]["options"][1] == {"text": "Massachusetts", "value": "MA", "selected": False}
    assert fields[4]["selector"] == 'input.acknowledgementCheck[value="yes"]'
    assert fields[4]["label"] == "I agree"
    assert fields[4]["disabled"] is False
    assert parsed["forms"][0]["submit_controls"][0]["selector"] == "#btnSubmit"
    assert parsed["forms"][0]["submit_controls"][0]["value"] == "Search"
    assert parsed["forms"][0]["submit_controls"][0]["disabled"] is False


def test_composition_parse_html_adds_challenge_state_for_anti_bot_dom() -> None:
    parsed = parse_composition_html(
        """
        <html><head><title>Just a moment...</title></head><body>
          <script src="https://verification.example/challenge.js"></script>
          <div class="human-verification">Verify you are human</div>
        </body></html>
        """,
        inspected_url="https://example.com/search",
        current_url="https://example.com/search",
    )

    assert page_evidence_needs_visual_fallback(parsed) is True
    assert "verify you are human" in parsed["anti_bot_indicators"]
    assert parsed["challenge_state"]["detected"] is True
    assert parsed["challenge_state"]["kind"] == "human_verification"
    assert parsed["challenge_state"]["source"] == "dom_html"
    assert parsed["challenge_state"]["gates_submit_controls"] is False
    assert parsed["challenge_state"]["gated_submit_controls"] == []


def test_composition_parse_html_reports_schema_empty_without_semantic_terminal_inference() -> None:
    parsed = parse_composition_html(
        """
        <html><head><title>Done</title></head><body>
          <main>Confirmation complete.</main>
        </body></html>
        """,
        inspected_url="https://example.com/confirmation",
        current_url="https://example.com/confirmation",
    )

    assert parsed["forms"] == []
    assert parsed["navigation_targets"] == []
    assert parsed["result_containers"] == []
    assert parsed["schema_empty_page"] is True
    assert parsed["observed_empty_page"] is False
    assert parsed["empty_page_visual_state"] is None
    assert "empty_page_state" not in parsed


def test_composition_parse_html_keeps_loading_shell_unobserved_without_visual_confirmation() -> None:
    parsed = parse_composition_html(
        """
        <html><head><title>Loading</title></head><body>
          <main>Loading...</main>
        </body></html>
        """,
        inspected_url="https://example.com/results",
        current_url="https://example.com/results",
    )

    assert parsed["forms"] == []
    assert parsed["navigation_targets"] == []
    assert parsed["result_containers"] == []
    assert parsed["schema_empty_page"] is True
    assert parsed["observed_empty_page"] is False
    assert parsed["empty_page_visual_state"] is None
    assert "empty_page_state" not in parsed


def test_visual_summary_marks_observed_empty_page_without_text_hints() -> None:
    parsed = parse_composition_html(
        """
        <html><head><title>Receipt</title></head><body></body></html>
        """,
        inspected_url="https://example.com/receipt",
        current_url="https://example.com/receipt",
    )

    marked = merge_visual_composition_evidence(
        parsed,
        visual_summary={
            "summary": "A settled blank page is visible after the submit action.",
            "challenge_detected": False,
            "submit_blocked": False,
            "empty_page_visible": True,
            "loading_state_visible": False,
        },
    )

    assert marked["observed_empty_page"] is True
    assert marked["empty_page_observation_source"] == "vision_summary"
    assert marked["empty_page_visual_state"] == "settled_empty"


def test_visual_summary_keeps_loading_shell_unobserved() -> None:
    parsed = parse_composition_html(
        """
        <html><head><title>Loading</title></head><body>
          <main>Loading...</main>
        </body></html>
        """,
        inspected_url="https://example.com/results",
        current_url="https://example.com/results",
    )

    marked = merge_visual_composition_evidence(
        parsed,
        visual_summary={
            "summary": "The page is still rendering and shows a wait state.",
            "challenge_detected": False,
            "submit_blocked": False,
            "empty_page_visible": False,
            "loading_state_visible": True,
        },
    )

    assert marked["observed_empty_page"] is False
    assert marked["empty_page_visual_state"] == "loading_or_progress"


def test_merge_visual_composition_evidence_keeps_screenshot_bounded_and_typed() -> None:
    parsed = parse_composition_html(
        "<html><head><title>Just a moment...</title></head><body>Human verification</body></html>",
        inspected_url="https://example.com/search",
        current_url="https://example.com/search",
    )

    merged = merge_visual_composition_evidence(
        parsed,
        visual_summary={
            "summary": "A human verification card is visible below the search form.",
            "challenge_detected": True,
            "challenge_kind": "human_verification",
            "challenge_location": "Below the acknowledgement checkbox and above the Search button.",
            "submit_blocked": True,
            "blocked_submit_controls": ["Search"],
            "page_obstruction_detected": True,
            "obstruction_kind": "verification_panel",
            "obstruction_location": "Centered above the search form.",
            "underlying_page_blocked": True,
            "visible_dismiss_controls": ["Continue"],
            "omissions": ["Result rows are not visible before verification."],
        },
    )

    assert merged["evidence_sources"] == ["dom_html", "screenshot", "vision_summary"]
    assert merged["screenshot_used"] is True
    assert merged["visual_evidence_summary"] == "A human verification card is visible below the search form."
    assert merged["challenge_state"]["source"] == "dom+screenshot"
    assert (
        merged["challenge_state"]["visual_location"]
        == "Below the acknowledgement checkbox and above the Search button."
    )
    assert merged["challenge_state"]["gates_submit_controls"] is True
    assert merged["challenge_state"]["gated_submit_controls"] == [{"text": "Search", "disabled": True}]
    assert merged["page_obstructions"] == [
        {
            "kind": "verification_panel",
            "source": "vision_summary",
            "visual_location": "Centered above the search form.",
            "visible_controls": [{"text": "Continue"}],
            "underlying_page_blocked": True,
        }
    ]
    assert merged["visual_evidence_omissions"] == ["Result rows are not visible before verification."]


def test_merge_visual_composition_evidence_keeps_false_underlying_page_blocked() -> None:
    parsed = parse_composition_html(
        "<html><head><title>Search</title></head><body><form><input name='q' /></form></body></html>",
        inspected_url="https://example.com/search",
        current_url="https://example.com/search",
    )

    merged = merge_visual_composition_evidence(
        parsed,
        visual_summary={
            "summary": "A banner is visible but the search form remains usable.",
            "page_obstruction_detected": True,
            "obstruction_kind": "banner",
            "obstruction_location": "Bottom of viewport.",
            "underlying_page_blocked": False,
        },
    )

    assert merged["page_obstructions"][0]["underlying_page_blocked"] is False


def test_composition_parse_html_surfaces_human_verification_controls_after_long_page_preamble() -> None:
    parsed = parse_composition_html(
        f"""
        <html><head><title>Credential Registry</title></head><body>
          <form id="searchForm">
            <input class="credentialTypeChoice" type="radio" name="credentialType[]" value="STANDARD" /> Standard
            <input id="id-first_name" name="first_name" type="text" />
            <input id="id-last_name" name="last_name" type="text" />
            <input class="acknowledgementCheck" type="checkbox" name="acknowledgement" value="yes" /> I agree
            <input id="btnSubmit" name="btnSubmit" type="button" value="Search" disabled />
          </form>
          <div>{"x" * 5000}</div>
          <script src="https://verification.example/challenge.js"></script>
          <div id="human-verification-widget" class="human-verification challenge-widget"
               data-callback="verificationSuccess"></div>
          <input type="hidden" name="human-verification-response" id="human-verification-response" />
        </body></html>
        """,
        inspected_url="https://example.com/registry/search",
        current_url="https://example.com/registry/search",
    )

    assert {
        "challenge",
        "human-verification",
    }.issubset(set(parsed["anti_bot_indicators"]))
    assert {control["selector"] for control in parsed["challenge_controls"]} >= {
        "#human-verification-widget",
        "#human-verification-response",
    }
    assert parsed["forms"][0]["submit_controls"][0]["selector"] == "#btnSubmit"
    assert parsed["forms"][0]["submit_controls"][0]["disabled"] is True
    assert parsed["challenge_state"]["gates_submit_controls"] is True
    assert parsed["challenge_state"]["gated_submit_controls"] == [
        {
            "text": "Search",
            "id": "btnSubmit",
            "name": "btnSubmit",
            "selector": "#btnSubmit",
            "disabled": True,
        }
    ]


def test_composition_gate_requires_page_evidence_before_page_dependent_blocks() -> None:
    goto_block = {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"}
    search_block = {
        "block_type": "navigation",
        "label": "search_lookup",
        "navigation_goal": "Enter the person name into the name search field and click Search.",
    }

    assert composition_page_evidence_error(_Ctx(), _yaml(goto_block)) is None

    error = composition_page_evidence_error(_Ctx(), _yaml(goto_block, search_block))

    assert error is not None
    assert "inspect_page_for_composition" in error
    assert "save only the initial goto_url block" in error
    assert "search_lookup" in error


def test_composition_gate_requires_page_evidence_before_no_url_action_blocks() -> None:
    # action / file_download / file_upload act on the reached page like a no-url navigation,
    # and the KB steers single clicks toward `action`, so they must be gated the same way.
    goto_block = {"block_type": "goto_url", "label": "open_cart", "url": "https://example.com/cart"}
    for acting_type in ("action", "file_download", "file_upload"):
        acting_block = {
            "block_type": acting_type,
            "label": f"do_{acting_type}",
            "navigation_goal": "Click the Add to cart button on the current page.",
        }

        error = composition_page_evidence_error(_Ctx(), _yaml(goto_block, acting_block))

        assert error is not None, f"{acting_type} should require page evidence"
        assert f"do_{acting_type}" in error


def test_composition_gate_names_extraction_only_blocks_missing_evidence() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_results", "url": "https://example.com/results"},
        {
            "block_type": "extraction",
            "label": "extract_results",
            "data_extraction_goal": "Extract the visible result rows.",
        },
    )

    error = composition_page_evidence_error(_Ctx(), workflow_yaml)

    assert error is not None
    assert "page-dependent blocks" in error
    assert "navigation/login" not in error
    assert "extract_results (extraction)" in error


def test_composition_gate_rejects_stale_page_evidence_from_another_origin() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Enter {{ parameters.person_name }} into the name search field and submit.",
        },
    )
    evidence = {
        **_first_last_evidence(),
        "inspected_url": "https://other.example/lookup",
        "current_url": "https://other.example/lookup",
    }

    error = composition_page_evidence_error(_Ctx(composition_page_evidence=evidence), workflow_yaml)

    assert error is not None
    assert "page-dependent build blocks need observed page evidence" in error


def test_composition_gate_rejects_stale_page_evidence_from_same_origin_different_path() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Enter {{ parameters.person_name }} into the name search field and submit.",
        },
    )
    evidence = {
        **_first_last_evidence(),
        "inspected_url": "https://example.com/login",
        "current_url": "https://example.com/login",
    }

    error = composition_page_evidence_error(_Ctx(composition_page_evidence=evidence), workflow_yaml)

    assert error is not None
    assert "page-dependent build blocks need observed page evidence" in error


def test_composition_gate_rejects_untyped_browser_observation_evidence() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Enter {{ parameters.person_name }} into the name search field and submit.",
        },
    )
    evidence = {
        "inspected_url": "https://example.com/lookup",
        "current_url": "https://example.com/lookup",
        "forms": [],
        "source_tool": "get_browser_screenshot",
    }

    error = composition_page_evidence_error(_Ctx(composition_page_evidence=evidence), workflow_yaml)

    assert error is not None
    assert "inspect_page_for_composition" in error


def test_composition_gate_rejects_precompose_screenshot_evidence_outside_inspection_tool() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Enter the observed first-name and last-name fields and submit.",
        },
    )
    evidence = {
        "inspected_url": "https://example.com/lookup",
        "current_url": "https://example.com/lookup",
        "forms": [],
        "source_tool": "get_browser_screenshot",
        "evidence_sources": ["screenshot", "vision_summary"],
        "screenshot_used": True,
        "visual_evidence_summary": "A search form is visible.",
    }

    error = composition_page_evidence_error(_Ctx(composition_page_evidence=evidence), workflow_yaml)

    assert error is not None
    assert "inspect_page_for_composition" in error


def test_composition_gate_accepts_screenshot_evidence_from_inspection_tool() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Enter the observed first-name and last-name fields and submit.",
        },
    )
    evidence = {
        **_first_last_evidence(),
        "evidence_sources": ["dom_html", "screenshot", "vision_summary"],
        "screenshot_used": True,
        "visual_evidence_summary": "A challenge is visible below the search form.",
    }

    error = composition_page_evidence_error(_Ctx(composition_page_evidence=evidence), workflow_yaml)

    assert error is None


def test_composition_gate_accepts_structured_evaluate_evidence_on_target_page() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Enter the observed First Name and Last Name fields and submit.",
        },
    )
    evidence = {
        "inspected_url": "https://example.com/lookup",
        "current_url": "https://example.com/lookup",
        "source_tool": "evaluate",
        "evidence_sources": ["mcp_evaluate"],
        "forms": [
            {
                "fields": [_field("First Name", "firstName"), _field("Last Name", "lastName")],
                "submit_controls": [{"text": "Search", "selector": "#search"}],
            }
        ],
    }

    error = composition_page_evidence_error(_Ctx(composition_page_evidence=evidence), workflow_yaml)

    assert error is None


def test_composition_gate_accepts_structured_evaluate_same_origin_after_initial_block() -> None:
    existing_yaml = _yaml({"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"})
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Enter the observed First Name and Last Name fields and submit.",
        },
    )
    evidence = {
        "inspected_url": "https://example.com/lookup?s=1&firstSubmit=1",
        "current_url": "https://example.com/lookup?s=1&firstSubmit=1",
        "source_tool": "evaluate",
        "evidence_sources": ["mcp_evaluate"],
        "forms": [{"fields": [_field("First Name", "firstName")], "submit_controls": []}],
    }
    ctx = _Ctx(composition_page_evidence=evidence)
    ctx.workflow_yaml = existing_yaml

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is None


def test_composition_gate_rejects_post_run_browser_observation_outside_inspection_tool() -> None:
    existing_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Enter the observed First Name and Last Name fields and submit.",
        },
    )
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Enter the observed First Name and Last Name fields and submit.",
        },
        {
            "block_type": "navigation",
            "label": "expand_result",
            "navigation_goal": "Click the observed result-row expansion control.",
        },
    )
    evidence = {
        "inspected_url": "https://example.com/results?id=123",
        "current_url": "https://example.com/results?id=123",
        "forms": [],
        "source_tool": "get_browser_screenshot",
        "observed_after_workflow_run": True,
    }
    ctx = _Ctx(composition_page_evidence=evidence)
    ctx.workflow_yaml = existing_yaml

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is not None
    assert "inspect_page_for_composition" in error


def test_composition_gate_allows_structured_evaluate_evidence_for_same_origin_continuation() -> None:
    existing_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Enter the observed First Name and Last Name fields and submit.",
        },
    )
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Enter the observed First Name and Last Name fields and submit.",
        },
        {
            "block_type": "navigation",
            "label": "expand_result",
            "navigation_goal": "Click the observed result-row expansion control.",
        },
    )
    evidence = {
        "inspected_url": "https://example.com/results?id=123",
        "current_url": "https://example.com/results?id=123",
        "source_tool": "evaluate",
        "evidence_sources": ["mcp_evaluate"],
        "result_containers": [{"tag": "table", "selector": "#results"}],
        "observed_after_workflow_run": True,
    }
    ctx = _Ctx(composition_page_evidence=evidence)
    ctx.workflow_yaml = existing_yaml

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is None


def test_composition_gate_allows_post_run_current_page_schema_on_same_origin_continuation() -> None:
    existing_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Enter the observed First Name and Last Name fields and submit.",
        },
    )
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Enter the observed First Name and Last Name fields and submit.",
        },
        {
            "block_type": "navigation",
            "label": "expand_result",
            "navigation_goal": "Click the observed result-row expansion control.",
        },
    )
    evidence = {
        "inspected_url": "current_page",
        "current_url": "https://example.com/results?id=123",
        "forms": [],
        "result_containers": [{"selector": "#results"}],
        "source_tool": "inspect_page_for_composition",
        "observed_after_workflow_run": True,
    }
    ctx = _Ctx(composition_page_evidence=evidence)
    ctx.workflow_yaml = existing_yaml

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is None


def test_composition_gate_allows_multiple_new_page_changing_blocks_from_one_observation() -> None:
    existing_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "open_form",
            "navigation_goal": "Open the observed lookup form.",
        },
    )
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "open_form",
            "navigation_goal": "Open the observed lookup form.",
        },
        {
            "block_type": "navigation",
            "label": "submit_search",
            "navigation_goal": "Fill the observed first-name and last-name fields and submit.",
        },
        {
            "block_type": "navigation",
            "label": "expand_result",
            "navigation_goal": "Click the result-row expansion control.",
        },
    )
    evidence = {
        "inspected_url": "current_page",
        "current_url": "https://example.com/lookup",
        "forms": [{"fields": [{"name": "first_name", "selector": "#first_name"}], "submit_controls": []}],
        "source_tool": "inspect_page_for_composition",
        "observed_after_workflow_run": True,
    }
    ctx = _Ctx(composition_page_evidence=evidence)
    ctx.workflow_yaml = existing_yaml

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is None


def test_composition_gate_rejects_hollow_inspect_evidence() -> None:
    # A pre-render shell parses to empty forms/links/result containers. An inspect
    # that captured nothing is not observation, so a page-acting block on that URL
    # stays gated — URL match alone must not satisfy the gate (SKY-10562).
    existing_yaml = _yaml({"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"})
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Fill the observed search fields and submit.",
        },
    )
    evidence = {
        "inspected_url": "https://example.com/lookup",
        "current_url": "https://example.com/lookup",
        "forms": [],
        "navigation_targets": [],
        "result_containers": [],
        "source_tool": "inspect_page_for_composition",
    }
    ctx = _Ctx(composition_page_evidence=evidence)
    ctx.workflow_yaml = existing_yaml

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is not None
    assert "observed page evidence" in error


def test_composition_gate_allows_extraction_added_with_new_page_changing_block() -> None:
    existing_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "submit_search",
            "navigation_goal": "Fill the observed search form and submit.",
        },
    )
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "submit_search",
            "navigation_goal": "Fill the observed search form and submit.",
        },
        {
            "block_type": "navigation",
            "label": "expand_result",
            "navigation_goal": "Click the observed result-row expansion control.",
        },
        {
            "block_type": "extraction",
            "label": "extract_expanded_result",
            "data_extraction_goal": "Extract the values visible in the expanded row.",
        },
    )
    evidence = {
        "inspected_url": "current_page",
        "current_url": "https://example.com/results",
        "result_containers": [{"selector": "#results"}],
        "source_tool": "inspect_page_for_composition",
        "observed_after_workflow_run": True,
    }
    ctx = _Ctx(composition_page_evidence=evidence)
    ctx.workflow_yaml = existing_yaml

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is None


def test_composition_gate_allows_extraction_after_matching_current_page_evidence() -> None:
    existing_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "submit_search",
            "navigation_goal": "Fill the observed search form and submit.",
        },
    )
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "submit_search",
            "navigation_goal": "Fill the observed search form and submit.",
        },
        {
            "block_type": "extraction",
            "label": "extract_visible_results",
            "data_extraction_goal": "Extract the values visible on the observed results page.",
        },
    )
    evidence = {
        "inspected_url": "current_page",
        "current_url": "https://example.com/results",
        "result_containers": [{"selector": "#results"}],
        "source_tool": "inspect_page_for_composition",
        "observed_after_workflow_run": True,
    }
    ctx = _Ctx(composition_page_evidence=evidence)
    ctx.workflow_yaml = existing_yaml

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is None


def test_composition_gate_rejects_post_budget_result_url_as_new_goto_url() -> None:
    existing_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {
            "block_type": "navigation",
            "label": "search_sample_record",
            "url": "https://example.com/lookup",
            "navigation_goal": "Fill the observed first-name and last-name fields and submit.",
        },
    )
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {
            "block_type": "navigation",
            "label": "search_sample_record",
            "url": "https://example.com/lookup",
            "navigation_goal": "Fill the observed first-name and last-name fields and submit.",
        },
        {
            "block_type": "goto_url",
            "label": "open_sample_record_detail",
            "url": "https://example.com/lookup?record_id=494764",
        },
        {
            "block_type": "extraction",
            "label": "extract_credential_details",
            "data_extraction_goal": "Extract visible credential details.",
        },
    )
    evidence = {
        "inspected_url": "current_page",
        "current_url": "https://example.com/lookup?record_id=494764",
        "result_containers": [{"selector": "#results"}],
        "source_tool": "inspect_page_for_composition",
        "observed_after_workflow_run": True,
    }
    ctx = _Ctx(composition_page_evidence=evidence)
    ctx.workflow_yaml = existing_yaml
    ctx.per_tool_budget_problem_block_labels = ["search_sample_record"]

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is not None
    assert "post-run browser URL" in error
    assert "open_sample_record_detail" in error
    assert "split or replace the budgeted frontier" in error


def test_composition_gate_rejects_post_budget_path_result_url_as_new_goto_url() -> None:
    existing_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_person",
            "navigation_goal": "Submit the observed search form.",
        },
    )
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_person",
            "navigation_goal": "Submit the observed search form.",
        },
        {
            "block_type": "goto_url",
            "label": "open_result_detail",
            "url": "https://example.com/results/494764",
        },
    )
    evidence = {
        "inspected_url": "current_page",
        "current_url": "https://example.com/results/494764",
        "source_tool": "inspect_page_for_composition",
        "observed_after_workflow_run": True,
    }
    ctx = _Ctx(composition_page_evidence=evidence)
    ctx.workflow_yaml = existing_yaml
    ctx.per_tool_budget_problem_block_labels = ["search_person"]

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is not None
    assert "open_result_detail" in error


def test_composition_gate_allows_extraction_from_post_budget_current_page() -> None:
    existing_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {
            "block_type": "navigation",
            "label": "search_sample_record",
            "url": "https://example.com/lookup",
            "navigation_goal": "Fill the observed first-name and last-name fields and submit.",
        },
    )
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {
            "block_type": "navigation",
            "label": "search_sample_record",
            "url": "https://example.com/lookup",
            "navigation_goal": "Fill the observed first-name and last-name fields and submit.",
        },
        {
            "block_type": "extraction",
            "label": "extract_visible_credentials",
            "data_extraction_goal": "Extract credential details visible on the observed current page.",
        },
    )
    evidence = {
        "inspected_url": "current_page",
        "current_url": "https://example.com/lookup?record_id=494764",
        "result_containers": [{"selector": "#results"}],
        "source_tool": "inspect_page_for_composition",
        "observed_after_workflow_run": True,
    }
    ctx = _Ctx(composition_page_evidence=evidence)
    ctx.workflow_yaml = existing_yaml
    ctx.per_tool_budget_problem_block_labels = ["search_sample_record"]

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is None


def test_composition_gate_targets_nearest_url_before_new_page_block() -> None:
    existing_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {
            "block_type": "goto_url",
            "label": "open_find_record",
            "url": "https://example.com/registry/",
        },
    )
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {
            "block_type": "goto_url",
            "label": "open_find_record",
            "url": "https://example.com/registry/",
        },
        {
            "block_type": "navigation",
            "label": "search_standard_record",
            "url": "https://example.com/registry/search",
            "navigation_goal": "Enter the observed First Name and Last Name fields and submit.",
        },
    )
    evidence = {
        "inspected_url": "https://example.com/registry/search",
        "current_url": "https://example.com/registry/search",
        "forms": [{"fields": [{"name": "first_name", "selector": "#first_name"}], "submit_controls": []}],
        "source_tool": "inspect_page_for_composition",
    }
    ctx = _Ctx(composition_page_evidence=evidence)
    ctx.workflow_yaml = existing_yaml

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is None


def test_composition_gate_error_names_nearest_url_before_new_page_block() -> None:
    existing_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {
            "block_type": "goto_url",
            "label": "open_find_record",
            "url": "https://example.com/registry/",
        },
    )
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {
            "block_type": "goto_url",
            "label": "open_find_record",
            "url": "https://example.com/registry/",
        },
        {
            "block_type": "navigation",
            "label": "search_standard_record",
            "url": "https://example.com/registry/search",
            "navigation_goal": "Enter the observed First Name and Last Name fields and submit.",
        },
    )
    ctx = _Ctx(composition_page_evidence=None)
    ctx.workflow_yaml = existing_yaml

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is not None
    assert "target_url='https://example.com/registry/search'" in error


def test_composition_gate_rejects_same_origin_browser_observation_before_run_continuation() -> None:
    existing_yaml = _yaml({"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"})
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Enter the observed First Name and Last Name fields and submit.",
        },
    )
    evidence = {
        "inspected_url": "https://example.com/lookup",
        "current_url": "https://example.com/lookup",
        "forms": [],
        "source_tool": "get_browser_screenshot",
        "observed_after_workflow_run": False,
    }
    ctx = _Ctx(composition_page_evidence=evidence)
    ctx.workflow_yaml = existing_yaml

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is not None
    assert "inspect_page_for_composition" in error


def test_composition_gate_applies_to_edit_turns_that_add_page_dependent_blocks() -> None:
    existing_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
    )
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Enter {{ parameters.person_name }} into the name search field and submit.",
        },
    )
    ctx = _Ctx(
        turn_intent=TurnIntent(mode=TurnIntentMode.EDIT),
        composition_page_evidence=None,
        workflow_yaml=existing_yaml,
    )

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is not None
    assert "search_lookup" in error


def test_composition_gate_rejects_page_dependent_blocks_without_target_url() -> None:
    workflow_yaml = _yaml(
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": "Enter {{ parameters.person_name }} into the name search field and submit.",
        },
    )

    error = composition_page_evidence_error(_Ctx(composition_page_evidence=_first_last_evidence()), workflow_yaml)

    assert error is not None
    assert "target_url=None" in error


def test_composition_gate_allows_navigation_after_matching_evidence() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "search_lookup",
            "navigation_goal": (
                "Enter the observed First Name and Last Name fields, then click the observed Search button."
            ),
            "complete_criterion": "The search results table is visible.",
        },
    )

    error = composition_page_evidence_error(_Ctx(composition_page_evidence=_first_last_evidence()), workflow_yaml)

    assert error is None


def test_composition_gate_allows_separate_form_state_and_submit_blocks_from_one_observation() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "prepare_lookup",
            "navigation_goal": (
                "Enter the observed First Name and Last Name fields. Stop with the Search button visible; "
                "do not submit the form."
            ),
        },
        {
            "block_type": "navigation",
            "label": "submit_lookup",
            "navigation_goal": "Click the observed Search button and wait for the result page.",
        },
        {
            "block_type": "extraction",
            "label": "extract_results",
            "data_extraction_goal": "Extract the credential rows from the result page.",
        },
    )

    error = composition_page_evidence_error(_Ctx(composition_page_evidence=_first_last_evidence()), workflow_yaml)

    assert error is None


def test_composition_gate_allows_navigation_split_blocks_sharing_entrypoint_observation_ref() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {
            "block_type": "navigation",
            "label": "prepare_lookup",
            "navigation_goal": "Enter the observed First Name and Last Name fields without submitting.",
        },
        {
            "block_type": "navigation",
            "label": "submit_lookup",
            "navigation_goal": "Click the observed Search button and wait for results.",
        },
        {
            "block_type": "extraction",
            "label": "extract_results",
            "data_extraction_goal": "Extract the credential rows from the result page.",
        },
    )
    ctx = _Ctx(
        flow_evidence=[_flow_entry("https://example.com/lookup", reached_via="navigate", step=0)],
        block_observation_refs={
            "prepare_lookup": 0,
            "submit_lookup": 0,
            "extract_results": 0,
        },
    )

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is None


# ---------------- SKY-10562: block-type-agnostic, per-acted-page, multi-page gate ----------------


def test_composition_gate_gates_non_entrypoint_goto_url_block() -> None:
    # A goto_url past the entrypoint acts on its own page and must be observed —
    # closing the goto_url block-type escape.
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "goto_url", "label": "open_cart", "url": "https://example.com/cart"},
        {"block_type": "validation", "label": "confirm_item", "complete_criterion": "An item is in the cart."},
    )

    error = composition_page_evidence_error(_Ctx(), workflow_yaml)
    assert error is not None
    assert "open_cart (goto_url)" in error

    ctx = _Ctx(flow_evidence=[_flow_entry("https://example.com/cart")])
    assert composition_page_evidence_error(ctx, workflow_yaml) is None


def test_composition_gate_entrypoint_goto_url_stays_ungated() -> None:
    # The first goto_url is the scaffold the agent scouts from — never gated.
    workflow_yaml = _yaml({"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"})
    assert composition_page_evidence_error(_Ctx(), workflow_yaml) is None


def test_composition_gate_pure_code_block_is_ungated() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "code", "label": "transform", "code": "result = 1 + 1"},
    )
    assert composition_page_evidence_error(_Ctx(), workflow_yaml) is None


def test_composition_gate_multi_page_flow_evidence_grounds_each_acted_page() -> None:
    # Two acted pages (login then a goto_url to /secure); the single-slot evidence
    # could only hold one, but the flow trajectory covers both.
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_login", "url": "https://example.com/login"},
        {"block_type": "login", "label": "do_login", "navigation_goal": "Log in with the saved credential."},
        {"block_type": "goto_url", "label": "open_secure", "url": "https://example.com/secure"},
        {"block_type": "validation", "label": "confirm_secure", "complete_criterion": "Secure area is shown."},
    )
    only_login = _Ctx(flow_evidence=[_flow_entry("https://example.com/login")])
    error = composition_page_evidence_error(only_login, workflow_yaml)
    assert error is not None
    assert "open_secure (goto_url)" in error

    both = _Ctx(
        flow_evidence=[
            _flow_entry("https://example.com/login"),
            _flow_entry("https://example.com/secure", reached_via="post_run"),
        ]
    )
    assert composition_page_evidence_error(both, workflow_yaml) is None


def test_composition_gate_requires_block_observation_refs_for_click_reached_pages() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "action", "label": "search_product", "navigation_goal": "Search for the product."},
        {"block_type": "action", "label": "add_first_result", "navigation_goal": "Add the first result to the cart."},
        {"block_type": "extraction", "label": "read_cart", "data_extraction_goal": "Read the cart contents."},
    )
    ctx = _Ctx(
        flow_evidence=[_flow_entry("https://example.com/", step=0)],
        block_observation_refs={"search_product": 0},
    )

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is not None
    assert "requires a block_observation_refs entry" in error
    assert "Pass an interaction- or post_run-reached observation_step" in error
    assert "add_first_result (action)" in error


def test_composition_gate_rejects_click_reached_blocks_reusing_entrypoint_observation_ref() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "action", "label": "search_product", "navigation_goal": "Search for the product."},
        {"block_type": "action", "label": "add_first_result", "navigation_goal": "Add the first result to the cart."},
        {"block_type": "extraction", "label": "read_cart", "data_extraction_goal": "Read the cart contents."},
    )
    ctx = _Ctx(
        flow_evidence=[_flow_entry("https://example.com/", reached_via="navigate", step=0)],
        block_observation_refs={
            "search_product": 0,
            "add_first_result": 0,
            "read_cart": 0,
        },
    )

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is not None
    assert "references observation_step 0" in error
    assert "reached via 'navigate'" in error
    assert "add_first_result (action)" in error


def test_composition_gate_allows_current_page_read_after_matching_interaction_reached_page() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "action", "label": "search_product", "navigation_goal": "Search for the product."},
        {"block_type": "action", "label": "add_first_result", "navigation_goal": "Add the first result to the cart."},
    )
    ctx = _Ctx(
        flow_evidence=[
            _flow_entry("https://example.com/", reached_via="navigate", step=0),
            _flow_entry("https://example.com/results?s=1", reached_via="interaction", step=1),
            _flow_entry("https://example.com/results?s=1", reached_via="current_page", step=2),
        ],
        block_observation_refs={
            "search_product": 0,
            "add_first_result": 2,
        },
    )

    assert composition_page_evidence_error(ctx, workflow_yaml) is None


def test_composition_gate_accepts_scout_interaction_observation_for_click_reached_block() -> None:
    # SKY-10712: a successful scout interaction reaches the page, so a click-reached
    # block authors against it without a separate inspect_page_for_composition.
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "action", "label": "search_product", "navigation_goal": "Search for the product."},
        {"block_type": "action", "label": "add_to_cart", "navigation_goal": "Add the first result to the cart."},
    )
    ctx = _Ctx(
        flow_evidence=[
            _flow_entry("https://example.com/", reached_via="navigate", step=0),
            _scout_interaction_entry("https://example.com/results", step=1),
            _scout_interaction_entry("https://example.com/cart", step=2),
        ],
        block_observation_refs={"search_product": 1, "add_to_cart": 2},
    )

    assert composition_page_evidence_error(ctx, workflow_yaml) is None


def test_composition_gate_rejects_hollow_interaction_observation_without_schema() -> None:
    # Guard: a hollow inspect (interaction-reached but no schema and not a scout
    # interaction) must still be rejected — the relaxation is scoped to scout proof.
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "action", "label": "search_product", "navigation_goal": "Search for the product."},
        {"block_type": "action", "label": "add_to_cart", "navigation_goal": "Add the first result to the cart."},
    )
    hollow = _flow_entry("https://example.com/cart", reached_via="interaction", with_form=False, step=2)
    ctx = _Ctx(
        flow_evidence=[
            _flow_entry("https://example.com/", reached_via="navigate", step=0),
            _scout_interaction_entry("https://example.com/results", step=1),
            hollow,
        ],
        block_observation_refs={"search_product": 1, "add_to_cart": 2},
    )

    assert composition_page_evidence_error(ctx, workflow_yaml) is not None


def test_composition_gate_auto_credits_interaction_observation_without_a_ref() -> None:
    # SKY-10712 option 1: a click-reached action block with NO block_observation_refs entry is
    # auto-credited from the most-recent interaction-reached observation; the agent need not thread it.
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "action", "label": "search_product", "navigation_goal": "Search for the product."},
        {"block_type": "action", "label": "add_to_cart", "navigation_goal": "Click the Add to Cart button."},
    )
    ctx = _Ctx(
        flow_evidence=[
            _flow_entry("https://example.com/", reached_via="navigate", step=0),
            _scout_interaction_entry("https://example.com/", step=1),
        ],
        block_observation_refs={},
    )

    assert composition_page_evidence_error(ctx, workflow_yaml) is None


def test_composition_gate_rejects_click_reached_block_with_no_interaction_observation() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "action", "label": "search_product", "navigation_goal": "Search for the product."},
        {"block_type": "action", "label": "add_to_cart", "navigation_goal": "Click the Add to Cart button."},
    )
    ctx = _Ctx(
        flow_evidence=[_flow_entry("https://example.com/", reached_via="navigate", step=0)],
        block_observation_refs={},
    )

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is not None
    assert "add_to_cart (action)" in error


def test_composition_gate_auto_credit_consumes_each_interaction_once() -> None:
    # Two click-reached blocks need two distinct interaction observations (consume-once); the
    # SPA URL is identical across both, so binding is by trajectory order, never by url.
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "action", "label": "search_product", "navigation_goal": "Search for the product."},
        {"block_type": "action", "label": "add_to_cart", "navigation_goal": "Click the Add to Cart button."},
        {"block_type": "action", "label": "open_cart", "navigation_goal": "Click the cart icon."},
    )
    base = [_flow_entry("https://example.com/", reached_via="navigate", step=0)]

    one = _Ctx(
        flow_evidence=base + [_scout_interaction_entry("https://example.com/", step=1)], block_observation_refs={}
    )
    assert composition_page_evidence_error(one, workflow_yaml) is not None

    two = _Ctx(
        flow_evidence=base
        + [
            _scout_interaction_entry("https://example.com/", step=1),
            _scout_interaction_entry("https://example.com/", step=2),
        ],
        block_observation_refs={},
    )
    assert composition_page_evidence_error(two, workflow_yaml) is None


def test_composition_gate_reports_missing_referenced_observation_step() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "action", "label": "search_product", "navigation_goal": "Search for the product."},
        {"block_type": "action", "label": "add_first_result", "navigation_goal": "Add the first result to the cart."},
    )
    ctx = _Ctx(
        flow_evidence=[_flow_entry("https://example.com/", reached_via="navigate", step=0)],
        block_observation_refs={
            "search_product": 0,
            "add_first_result": 9,
        },
    )

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is not None
    assert "references observation_step 9" in error
    assert "observation step was not found in flow evidence" in error
    assert "add_first_result (action)" in error


def test_composition_gate_reports_evicted_referenced_observation_step() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "action", "label": "search_product", "navigation_goal": "Search for the product."},
        {"block_type": "action", "label": "add_first_result", "navigation_goal": "Add the first result to the cart."},
    )
    ctx = _Ctx(
        flow_evidence=[_flow_entry("https://example.com/cart", reached_via="interaction", step=65)],
        block_observation_refs={
            "search_product": 65,
            "add_first_result": 9,
        },
    )

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is not None
    assert "references observation_step 9" in error
    assert "no longer available in the flow-evidence window" in error


def test_normalize_block_observation_refs_rejects_string_steps() -> None:
    assert normalize_block_observation_refs(
        [
            {"label": "add_to_cart", "observation_step": 2},
            {"label": "confirm_cart", "observation_step": "3"},
        ]
    ) == {"add_to_cart": 2}


def test_normalize_block_observation_refs_warns_on_unexpected_container_type() -> None:
    with patch("skyvern.forge.sdk.copilot.composition_evidence.LOG.warning") as warning:
        assert normalize_block_observation_refs("add_to_cart:2") == {}

    warning.assert_called_once_with(
        "copilot_block_observation_refs_unexpected_type_ignored",
        value_type="str",
    )


def test_composition_gate_reports_string_typed_observation_step_from_raw_refs() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "action", "label": "search_product", "navigation_goal": "Search for the product."},
        {"block_type": "action", "label": "add_first_result", "navigation_goal": "Add the first result to the cart."},
    )
    ctx = _Ctx(
        # No interaction-reached observation exists, so auto-credit cannot ground the block and the
        # string-typed-ref diagnostic fires instead.
        flow_evidence=[
            _flow_entry("https://example.com/", reached_via="navigate", step=0),
        ],
        block_observation_refs={
            "search_product": 0,
        },
        raw_block_observation_refs=[
            {"label": "search_product", "observation_step": 0},
            {"label": "add_first_result", "observation_step": "1"},
        ],
    )

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is not None
    assert "observation_step '1' as a string" in error
    assert "Pass the integer observation_step" in error
    assert "add_first_result (action)" in error


def test_composition_gate_rejects_action_after_navigation_reusing_entrypoint_observation_ref() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "navigation", "label": "search_product", "navigation_goal": "Search for the product."},
        {"block_type": "action", "label": "add_first_result", "navigation_goal": "Add the first result to the cart."},
    )
    ctx = _Ctx(
        flow_evidence=[_flow_entry("https://example.com/", reached_via="navigate", step=0)],
        block_observation_refs={
            "search_product": 0,
            "add_first_result": 0,
        },
    )

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is not None
    assert "add_first_result (action)" in error


def test_composition_gate_allows_click_reached_pages_with_block_observation_refs() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "action", "label": "search_product", "navigation_goal": "Search for the product."},
        {"block_type": "action", "label": "add_first_result", "navigation_goal": "Add the first result to the cart."},
        {"block_type": "extraction", "label": "read_cart", "data_extraction_goal": "Read the cart contents."},
    )
    ctx = _Ctx(
        flow_evidence=[
            _flow_entry("https://example.com/", reached_via="navigate", step=0),
            _flow_entry("https://example.com/results", reached_via="interaction", step=1),
            _flow_entry("https://example.com/cart", reached_via="interaction", step=2),
        ],
        block_observation_refs={
            "search_product": 0,
            "add_first_result": 1,
            "read_cart": 2,
        },
    )

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is None


def test_composition_gate_allows_truthfully_empty_observed_confirmation_page() -> None:
    confirmation_evidence = merge_visual_composition_evidence(
        parse_composition_html(
            "<html><head><title>Blank receipt</title></head><body></body></html>",
            inspected_url="https://example.com/confirmation",
            current_url="https://example.com/confirmation",
        ),
        visual_summary={
            "summary": "The browser shows a settled blank receipt page.",
            "challenge_detected": False,
            "submit_blocked": False,
            "empty_page_visible": True,
            "loading_state_visible": False,
        },
    )
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "action", "label": "submit_form", "navigation_goal": "Submit the form."},
        {"block_type": "validation", "label": "confirm_done", "complete_criterion": "The confirmation page loaded."},
    )
    ctx = _Ctx(
        flow_evidence=[
            _flow_entry("https://example.com/", reached_via="navigate", step=0),
            {
                "evidence": confirmation_evidence,
                "reached_via": "interaction",
                "had_bounded_schema": True,
                "step": 1,
            },
        ],
        block_observation_refs={"submit_form": 0, "confirm_done": 1},
    )

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is None


def test_composition_gate_regates_changed_block_url() -> None:
    previous = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "navigation", "label": "open_page", "url": "https://example.com/old", "navigation_goal": "go"},
    )
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "navigation", "label": "open_page", "url": "https://example.com/new", "navigation_goal": "go"},
    )
    ctx = _Ctx(flow_evidence=[_flow_entry("https://example.com/old")])
    ctx.workflow_yaml = previous
    error = composition_page_evidence_error(ctx, workflow_yaml)
    assert error is not None
    assert "open_page (navigation)" in error

    ctx_observed = _Ctx(flow_evidence=[_flow_entry("https://example.com/new")])
    ctx_observed.workflow_yaml = previous
    assert composition_page_evidence_error(ctx_observed, workflow_yaml) is None


def test_composition_gate_credits_cross_turn_observed_page_summary() -> None:
    # A page observed on a prior turn (its inspection budget already spent) is
    # credited from the persisted summary so the gate does not deadlock.
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_lookup", "url": "https://example.com/lookup"},
        {"block_type": "navigation", "label": "search_lookup", "navigation_goal": "Fill and submit the form."},
    )
    ctx = _Ctx(
        prior_observed_acted_pages=[
            {"url": "https://example.com/lookup", "had_bounded_schema": True, "reached_via": "navigate"}
        ]
    )
    assert composition_page_evidence_error(ctx, workflow_yaml) is None


def test_composition_gate_cross_turn_credit_requires_same_page_not_origin() -> None:
    # A page observed on a prior turn credits only the SAME page, never a sibling
    # on the same origin — otherwise the gate would author an unobserved page's
    # block from a same-origin observation.
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "goto_url", "label": "open_admin", "url": "https://example.com/admin"},
        {"block_type": "validation", "label": "confirm_admin", "complete_criterion": "Admin panel is shown."},
    )
    sibling_only = _Ctx(
        prior_observed_acted_pages=[
            {"url": "https://example.com/lookup", "had_bounded_schema": True, "reached_via": "navigate"}
        ]
    )
    sibling_only.workflow_yaml = _yaml({"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"})
    error = composition_page_evidence_error(sibling_only, workflow_yaml)
    assert error is not None
    assert "open_admin (goto_url)" in error

    exact = _Ctx(
        prior_observed_acted_pages=[
            {"url": "https://example.com/admin", "had_bounded_schema": True, "reached_via": "navigate"}
        ]
    )
    exact.workflow_yaml = _yaml({"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"})
    assert composition_page_evidence_error(exact, workflow_yaml) is None


def test_composition_gate_matches_url_blocks_against_target_when_observation_ref_is_present() -> None:
    workflow_yaml = _yaml(
        {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
        {"block_type": "goto_url", "label": "open_cart", "url": "https://example.com/cart"},
    )
    ctx = _Ctx(
        flow_evidence=[_flow_entry("https://example.com/")],
        block_observation_refs={"open_cart": 0},
    )

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is not None
    assert "open_cart (goto_url)" in error


# Bounded structured-evidence extractor


def _structured_form_payload() -> dict[str, Any]:
    return {
        "page_title": "Lookup",
        "forms": [
            {
                "id": "searchForm",
                "name": "",
                "action": "/results",
                "method": "get",
                "fields": [
                    {
                        "name": "q",
                        "id": "q",
                        "label": "Full name",
                        "type": "text",
                        "value": "",
                        "class": [],
                        "placeholder": "name",
                        "required": True,
                        "disabled": False,
                        "checked": False,
                        "options": [],
                        "selector": "#q",
                    },
                    {
                        "name": "state",
                        "id": "state",
                        "label": "State",
                        "type": "select",
                        "value": "",
                        "class": [],
                        "placeholder": "",
                        "required": False,
                        "disabled": False,
                        "checked": False,
                        "options": [
                            {"text": "CA", "value": "ca", "selected": True},
                            {"text": "NY", "value": "ny", "selected": False},
                        ],
                        "selector": "#state",
                    },
                ],
                "submit_controls": [
                    {
                        "text": "Search",
                        "name": "",
                        "id": "",
                        "value": "",
                        "class": [],
                        "type": "submit",
                        "disabled": False,
                        "selector": "button",
                    }
                ],
            }
        ],
        "navigation_targets": [],
        "result_containers": [],
        "challenge_controls": [],
        "modal_overlays": [],
        "visual_obstruction_candidates": [],
        "visible_text_excerpt": "Full name State Search",
        "anti_bot_indicators": [],
    }


def test_structured_parses_forms_labels_options_and_submit() -> None:
    parsed = parse_composition_structured(
        _structured_form_payload(),
        inspected_url="https://example.com/lookup",
        current_url="https://example.com/lookup",
    )

    assert parsed is not None
    assert parsed["source_tool"] == "inspect_page_for_composition"
    form = parsed["forms"][0]
    assert [field["label"] for field in form["fields"]] == ["Full name", "State"]
    assert form["fields"][0]["required"] is True
    assert form["fields"][1]["options"][0] == {"text": "CA", "value": "ca", "selected": True}
    assert form["submit_controls"][0]["text"] == "Search"
    assert parsed["evidence_confidence"] == 0.85
    assert has_bounded_page_schema(parsed) is True


def test_structured_detects_modal_overlay_with_dismiss_controls() -> None:
    payload = {
        "page_title": "",
        "forms": [],
        "navigation_targets": [],
        "result_containers": [],
        "challenge_controls": [],
        "modal_overlays": [
            {
                "role": "dialog",
                "aria_modal": True,
                "id": "",
                "class": ["modal"],
                "selector": "div.modal",
                "text": "Subscribe",
                "dismiss_controls": [
                    {
                        "tag": "button",
                        "text": "Close",
                        "aria_label": "",
                        "title": "",
                        "selector": "button.x",
                        "type": "",
                    }
                ],
            }
        ],
        "visual_obstruction_candidates": [],
        "visible_text_excerpt": "Subscribe Close",
        "anti_bot_indicators": [],
    }

    parsed = parse_composition_structured(
        payload, inspected_url="https://example.com/r", current_url="https://example.com/r"
    )

    assert parsed is not None
    overlay = parsed["modal_overlays"][0]
    assert overlay["selector"] == "div.modal"
    assert overlay["dismiss_controls"][0]["text"] == "Close"
    assert "aria_modal" in overlay and overlay["aria_modal"] is True
    assert parsed["page_obstructions"]
    assert has_bounded_page_schema(parsed) is True


def test_structured_detects_anti_bot_and_challenge_controls() -> None:
    payload = {
        "page_title": "",
        "forms": [],
        "navigation_targets": [],
        "result_containers": [],
        "challenge_controls": [
            {
                "tag": "iframe",
                "id": "",
                "name": "",
                "class": [],
                "type": "",
                "selector": "iframe",
                "text": "",
                "src": "https://challenges.cloudflare.com/turnstile/v0/api.js",
                "title": "Cloudflare security challenge",
            }
        ],
        "modal_overlays": [],
        "visual_obstruction_candidates": [],
        "visible_text_excerpt": "",
        "anti_bot_indicators": ["captcha", "turnstile"],
    }

    parsed = parse_composition_structured(
        payload, inspected_url="https://example.com/x", current_url="https://example.com/x"
    )

    assert parsed is not None
    assert parsed["anti_bot_indicators"] == ["captcha", "turnstile"]
    assert parsed["challenge_state"]["detected"] is True
    assert parsed["challenge_state"]["kind"] == "captcha"
    assert "data_sitekey" not in parsed["challenge_controls"][0]
    assert parsed["challenge_controls"][0]["src"] == "https://challenges.cloudflare.com/turnstile/v0/api.js"
    assert has_bounded_page_schema(parsed) is True
    assert page_evidence_needs_visual_fallback(parsed) is True


def test_structured_rejects_unusable_payloads() -> None:
    for bad in (None, "not a dict", ["a"], 123, 4.5, True):
        assert parse_composition_structured(bad, inspected_url="u", current_url="u") is None


def test_structured_intersects_reported_anti_bot_indicators_with_known_patterns() -> None:
    payload = _structured_form_payload()
    payload["anti_bot_indicators"] = ["captcha", "totally-made-up", "challenge"]

    parsed = parse_composition_structured(payload, inspected_url="u", current_url="u")

    assert parsed is not None
    # Bogus indicators are dropped; survivors are returned in canonical pattern order.
    assert parsed["anti_bot_indicators"] == ["captcha", "challenge"]


def test_structured_navigation_drops_cross_origin_links() -> None:
    payload = _structured_form_payload()
    payload["navigation_targets"] = [
        {"text": "Same", "href": "https://example.com/page2", "selector": 'a[href="/page2"]'},
        {"text": "Cross", "href": "https://other.example.org/x", "selector": 'a[href="x"]'},
        {"text": "Scheme", "href": "http://example.com/page3", "selector": 'a[href="/page3"]'},
    ]

    parsed = parse_composition_structured(
        payload, inspected_url="https://example.com/lookup", current_url="https://example.com/lookup"
    )

    assert parsed is not None
    hrefs = [target["href"] for target in parsed["navigation_targets"]]
    # netloc match keeps the http<->https same-host link and drops the cross-host one.
    assert "https://example.com/page2" in hrefs
    assert "http://example.com/page3" in hrefs
    assert "https://other.example.org/x" not in hrefs


def test_structured_body_with_markup_but_no_structure_is_schema_empty() -> None:
    # body markup but no bounded structure and no visible text -> schema-empty.
    payload = {
        "page_title": "",
        "forms": [],
        "navigation_targets": [],
        "result_containers": [],
        "challenge_controls": [],
        "modal_overlays": [],
        "visual_obstruction_candidates": [],
        "visible_text_excerpt": "",
        "body_has_markup": True,
        "anti_bot_indicators": [],
    }

    parsed = parse_composition_structured(payload, inspected_url="u", current_url="u")

    assert parsed is not None
    assert parsed["schema_empty_page"] is True
    assert has_bounded_page_schema(parsed) is False


def test_structured_blank_payload_is_not_schema_empty() -> None:
    payload = {
        "page_title": "",
        "forms": [],
        "navigation_targets": [],
        "result_containers": [],
        "challenge_controls": [],
        "modal_overlays": [],
        "visual_obstruction_candidates": [],
        "visible_text_excerpt": "",
        "body_has_markup": False,
        "anti_bot_indicators": [],
    }

    parsed = parse_composition_structured(payload, inspected_url="u", current_url="u")

    assert parsed is not None
    assert parsed["schema_empty_page"] is False


# JS-DOM fidelity: run the real extractor against a live DOM and compare to the HTML parser


def _has_playwright_browser() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


_skip_no_browser = pytest.mark.skipif(
    not _has_playwright_browser(),
    reason="Requires Playwright browsers installed (run: playwright install chromium)",
)

_FIDELITY_URL = "https://test.example.com/lookup"
_FIDELITY_HTML = """<!DOCTYPE html>
<html>
<head><title>Lookup</title></head>
<body>
  <form id="searchForm" action="/results" method="get">
    <label for="q">Full name</label>
    <input id="q" name="q" required placeholder="name" />
    <label>Email <input name="email" type="email" /></label>
    <span>Phone</span><input name="phone" />
    <select id="state" name="state"><option value="ca" selected>CA</option><option value="ny">NY</option></select>
    <input type="hidden" name="csrf" value="xyz" />
    <button type="submit">Search</button>
  </form>
  <a href="/results">Results</a>
  <a href="https://test.example.com/page2">Page2</a>
  <a href="https://other.example.org/x">External</a>
  <a href="#frag">Frag</a>
  <table id="results"><tbody><tr><td>row</td></tr></tbody></table>
  <div role="dialog" aria-modal="true" class="modal" id="signup">Sign up<button aria-label="Close">x</button></div>
  <iframe src="https://challenges.cloudflare.com/turnstile/v0/api.js" title="Widget containing a Cloudflare security challenge"></iframe>
  <div class="cf-turnstile" data-sitekey="abc123">Verify</div>
  <script>var marker = "captcha widget loaded";</script>
  <noscript>access denied</noscript>
</body>
</html>
"""


def _ac_projection(evidence: dict[str, Any]) -> dict[str, Any]:
    """Project the acceptance-criteria fields (forms/labels/modal/anti-bot/nav/challenge) for parity."""
    forms = [
        {
            "fields": [(field["name"], field["label"], field["type"], field["required"]) for field in form["fields"]],
            "submit": [control["text"] for control in form["submit_controls"]],
        }
        for form in evidence["forms"]
    ]
    challenge_state = evidence["challenge_state"]
    return {
        "page_title": evidence["page_title"],
        "forms": forms,
        "navigation_targets": sorted(target["href"] for target in evidence["navigation_targets"]),
        "result_containers": sorted((rc["tag"], rc["selector"]) for rc in evidence["result_containers"]),
        "modal_selectors": sorted(
            (overlay["selector"], bool(overlay.get("dismiss_controls"))) for overlay in evidence["modal_overlays"]
        ),
        "challenge_selectors": sorted(control["selector"] for control in evidence["challenge_controls"]),
        "anti_bot_indicators": evidence["anti_bot_indicators"],
        "challenge_detected": challenge_state["detected"],
        "challenge_kind": challenge_state["kind"],
        "bounded": has_bounded_page_schema(evidence),
    }


@_skip_no_browser
@pytest.mark.asyncio
async def test_structured_extractor_matches_html_parser_on_live_dom() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        async def _handle(route: Any) -> None:
            if route.request.url == _FIDELITY_URL:
                await route.fulfill(status=200, content_type="text/html", body=_FIDELITY_HTML)
            else:
                await route.abort()

        await context.route("**/*", _handle)
        page = await context.new_page()
        await page.goto(_FIDELITY_URL, wait_until="domcontentloaded")
        await page.wait_for_selector("#searchForm")
        raw = await page.evaluate(COMPOSITION_STRUCTURED_EVIDENCE_EXPRESSION)
        content = await page.content()
        await context.close()
        await browser.close()

    structured = parse_composition_structured(json.loads(raw), inspected_url=_FIDELITY_URL, current_url=_FIDELITY_URL)
    html_parsed = parse_composition_html(content, inspected_url=_FIDELITY_URL, current_url=_FIDELITY_URL)

    assert structured is not None
    assert _ac_projection(structured) == _ac_projection(html_parsed)
    # Sanity: the fixture really exercised the detectors.
    assert structured["forms"] and structured["challenge_controls"]
    assert structured["anti_bot_indicators"] and structured["challenge_state"]["detected"] is True
    assert any(overlay["selector"] == "#signup" for overlay in structured["modal_overlays"])


_HEAVY_URL = "https://test.example.com/cart"


def _heavy_results_cart_html() -> str:
    """A heavy results+cart page: many rows, multiple forms/containers, lots of nav.

    Exercises extractor/parser parity at the scale where their `_MAX_*` caps must
    agree — the case SKY-10714 ships for, where get_html cap-overflows in prod.
    """
    product_rows = "".join(
        f"<tr><td>Item {i}</td><td>${i}.99</td>"
        f'<td><button name="add" value="{i}" type="button">Add to cart</button></td></tr>'
        for i in range(1, 41)
    )
    cart_rows = "".join(
        f"<tr><td>Line {i}</td>"
        f'<td><label for="qty{i}">Qty {i}</label><input id="qty{i}" name="qty{i}" value="1" /></td></tr>'
        for i in range(1, 16)
    )
    filter_fields = "".join(
        f'<label for="f{i}">Filter {i}</label><input id="f{i}" name="f{i}" />' for i in range(1, 13)
    )
    nav_links = "".join(f'<a href="/category/{i}">Category {i}</a>' for i in range(1, 26))
    return f"""<!DOCTYPE html>
<html><head><title>Cart</title></head>
<body>
  <form id="filterForm" action="/results" method="get">
    {filter_fields}
    <button type="submit">Apply filters</button>
  </form>
  <table id="products"><tbody>{product_rows}</tbody></table>
  <form id="cartForm" action="/cart" method="post">
    <table id="cart"><tbody>{cart_rows}</tbody></table>
    <button type="submit">Checkout</button>
  </form>
  <nav>{nav_links}<a href="https://other.example.org/x">External</a><a href="#top">Top</a></nav>
  <div role="dialog" aria-modal="true" class="modal" id="promo">Promo<button aria-label="Close">x</button></div>
</body></html>"""


@_skip_no_browser
@pytest.mark.asyncio
async def test_structured_extractor_matches_html_parser_on_heavy_results_cart_dom() -> None:
    from playwright.async_api import async_playwright

    html = _heavy_results_cart_html()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        async def _handle(route: Any) -> None:
            if route.request.url == _HEAVY_URL:
                await route.fulfill(status=200, content_type="text/html", body=html)
            else:
                await route.abort()

        await context.route("**/*", _handle)
        page = await context.new_page()
        await page.goto(_HEAVY_URL, wait_until="domcontentloaded")
        await page.wait_for_selector("#products")
        raw = await page.evaluate(COMPOSITION_STRUCTURED_EVIDENCE_EXPRESSION)
        content = await page.content()
        await context.close()
        await browser.close()

    structured = parse_composition_structured(json.loads(raw), inspected_url=_HEAVY_URL, current_url=_HEAVY_URL)
    html_parsed = parse_composition_html(content, inspected_url=_HEAVY_URL, current_url=_HEAVY_URL)

    assert structured is not None
    assert _ac_projection(structured) == _ac_projection(html_parsed)
    # Sanity: the heavy fixture really exercised results + multi-form at scale.
    assert structured["result_containers"]
    assert structured["forms"]


# Tools-layer invariant: cheap path skips get_html; failure falls back


class _RecordingCompositionServer:
    """Records call_internal_tool tool names and evaluate expressions for invariant assertions."""

    def __init__(self, *, structured_json: str | None, html: str = "") -> None:
        self.calls: list[str] = []
        self.evaluate_expressions: list[str] = []
        self._structured_json = structured_json
        self._html = html

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(tool_name)
        if tool_name == "skyvern_evaluate":
            expression = arguments.get("expression", "")
            self.evaluate_expressions.append(expression)
            if expression == COMPOSITION_STRUCTURED_EVIDENCE_EXPRESSION:
                if self._structured_json is None:
                    return {"ok": False, "error": "structured extract failed"}
                return {"ok": True, "data": {"result": self._structured_json}}
            if expression == COMPOSITION_VISUAL_OBSTRUCTION_CANDIDATES_EXPRESSION:
                return {"ok": True, "data": {"result": []}}
            return {"ok": False, "error": "unexpected expression"}
        if tool_name == "skyvern_get_html":
            return {"ok": True, "data": {"html": self._html}}
        return {"ok": False, "error": f"unexpected tool {tool_name}"}


_HTML_FORM_PAGE = (
    "<html><head><title>T</title></head><body>"
    "<form id='f'><input name='x'><button type='submit'>Go</button></form>"
    "</body></html>"
)


@pytest.mark.asyncio
async def test_capture_uses_structured_extractor_and_skips_get_html() -> None:
    server = _RecordingCompositionServer(structured_json=json.dumps(_structured_form_payload()))
    ctx = SimpleNamespace(discovery_mcp_server=server)

    evidence, error = await tools_module._capture_composition_evidence(
        ctx, inspected_url="https://example.com/lookup", current_url="https://example.com/lookup"
    )

    assert error is None
    assert evidence is not None
    assert evidence["forms"][0]["fields"][0]["label"] == "Full name"
    assert evidence["source_tool"] == "inspect_page_for_composition"
    # AC1: the cheap path never serializes the full DOM, and a bounded page needs only one evaluate.
    assert server.calls.count("skyvern_get_html") == 0
    assert server.calls.count("skyvern_evaluate") == 1


@pytest.mark.asyncio
async def test_capture_falls_back_to_get_html_when_structured_unusable() -> None:
    server = _RecordingCompositionServer(structured_json=None, html=_HTML_FORM_PAGE)
    ctx = SimpleNamespace(discovery_mcp_server=server)

    evidence, error = await tools_module._capture_composition_evidence(
        ctx, inspected_url="https://example.com/lookup", current_url="https://example.com/lookup"
    )

    assert error is None
    assert evidence is not None
    assert evidence["forms"]
    assert server.calls.count("skyvern_get_html") >= 1


@pytest.mark.asyncio
async def test_navigation_failure_uses_structured_evidence_when_bounded() -> None:
    server = _RecordingCompositionServer(structured_json=json.dumps(_structured_form_payload()))
    ctx = SimpleNamespace(discovery_mcp_server=server, browser_session_id=None, organization_id="o_test")

    evidence = await tools_module._composition_evidence_after_navigation_failure(
        ctx, inspected_url="https://example.com/lookup", navigation_error="boom"
    )

    assert evidence is not None
    assert evidence["forms"]
    assert server.calls.count("skyvern_get_html") == 0
    assert any("navigation_error_before_html_capture" in warning for warning in evidence["inspection_warnings"])


@pytest.mark.asyncio
async def test_capture_prefers_html_parse_over_hollow_structured_on_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # When both extractor and get_html are unbounded, the get_html parse wins (carries the fallback signals).
    monkeypatch.setattr(tools_module.composition_capture, "_COMPOSITION_HOLLOW_RECAPTURE_RETRIES", 0)
    hollow = json.dumps(
        {
            "page_title": "",
            "forms": [],
            "navigation_targets": [],
            "result_containers": [],
            "challenge_controls": [],
            "modal_overlays": [],
            "visual_obstruction_candidates": [],
            "visible_text_excerpt": "",
            "body_has_markup": False,
            "anti_bot_indicators": [],
        }
    )
    html = "<html><head><title>Notice</title></head><body><p>Welcome notice text</p></body></html>"
    server = _RecordingCompositionServer(structured_json=hollow, html=html)
    ctx = SimpleNamespace(discovery_mcp_server=server)

    evidence, error = await tools_module._capture_composition_evidence(
        ctx, inspected_url="https://example.com/p", current_url="https://example.com/p"
    )

    assert error is None
    assert evidence is not None
    assert "Welcome notice text" in evidence["visible_text_excerpt"]
    assert evidence["schema_empty_page"] is True
    assert server.calls.count("skyvern_get_html") == 1
