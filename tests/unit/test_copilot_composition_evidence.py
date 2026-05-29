"""Tests for evidence-grounded Copilot composition."""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from skyvern.forge.sdk.copilot.build_phase import BuildPhase
from skyvern.forge.sdk.copilot.composition_evidence import (
    composition_page_evidence_error,
    merge_visual_composition_evidence,
    page_evidence_needs_visual_fallback,
    parse_composition_html,
)
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentMode


@dataclass
class _Ctx:
    build_phase: BuildPhase = BuildPhase.COMPOSING
    turn_intent: TurnIntent = field(default_factory=lambda: TurnIntent(mode=TurnIntentMode.BUILD))
    composition_page_evidence: dict | None = None
    workflow_yaml: str | None = None


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
    assert merged["visual_evidence_omissions"] == ["Result rows are not visible before verification."]


def test_composition_parse_html_surfaces_turnstile_challenge_controls_after_long_page_preamble() -> None:
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
    ctx.workflow_yaml = existing_yaml  # type: ignore[attr-defined]

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
    ctx.workflow_yaml = existing_yaml  # type: ignore[attr-defined]

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
    ctx.workflow_yaml = existing_yaml  # type: ignore[attr-defined]

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
        "source_tool": "inspect_page_for_composition",
        "observed_after_workflow_run": True,
    }
    ctx = _Ctx(composition_page_evidence=evidence)
    ctx.workflow_yaml = existing_yaml  # type: ignore[attr-defined]

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
        "forms": [],
        "source_tool": "inspect_page_for_composition",
        "observed_after_workflow_run": True,
    }
    ctx = _Ctx(composition_page_evidence=evidence)
    ctx.workflow_yaml = existing_yaml  # type: ignore[attr-defined]

    error = composition_page_evidence_error(ctx, workflow_yaml)

    assert error is None


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
    ctx.workflow_yaml = existing_yaml  # type: ignore[attr-defined]

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
    ctx.workflow_yaml = existing_yaml  # type: ignore[attr-defined]

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
        "forms": [],
        "source_tool": "inspect_page_for_composition",
    }
    ctx = _Ctx(composition_page_evidence=evidence)
    ctx.workflow_yaml = existing_yaml  # type: ignore[attr-defined]

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
    ctx.workflow_yaml = existing_yaml  # type: ignore[attr-defined]

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
    ctx.workflow_yaml = existing_yaml  # type: ignore[attr-defined]

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
