"""Tests for the pure copilot code-block synthesizer.

OSS-synced: only example.* / RFC-2606 placeholder targets.
"""

from __future__ import annotations

import ast
import keyword
import sys
from typing import Any

from skyvern.forge.sdk.copilot.code_block_preflight import preflight_code_block
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    _MAX_STEPS,
    _SYNTHESIZED_BLOCK_LABEL,
    build_synthesized_artifact_metadata,
    render_synthesized_offer_text,
    synthesize_code_block,
)
from skyvern.forge.sdk.copilot.tools import _normalize_code_artifact_metadata


def _interaction(tool_name: str, **fields: Any) -> dict[str, Any]:
    return {"tool_name": tool_name, **fields}


class TestLocatorSynthesis:
    def test_role_selector_emits_get_by_role(self) -> None:
        # A `role=...` selector is an ARIA anchor (ref_to_selector form), not a native CSS path.
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector='role=button[name="Add to cart"]',
                    source_url="https://example.com/product",
                )
            ]
        )
        assert result is not None
        assert 'await page.get_by_role("button", name="Add to cart").click()' in result.code
        assert "get_by_role" in result.code

    def test_stable_id_selector_is_emitted_verbatim_not_get_by_role(self) -> None:
        # Selector-first: a captured stable selector (id) wins over a get_by_role anchor, because the
        # scout's a11y-name read may not reproduce on the raw page the code block runs against.
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector="#search-box",
                    source_url="https://example.com/",
                    typed_length=11,
                    role="textbox",
                    accessible_name="Search",
                )
            ]
        )
        assert result is not None
        assert 'await page.locator("#search-box").fill(str(search))' in result.code
        assert "get_by_role" not in result.code

    def test_stable_attribute_selector_is_emitted_verbatim(self) -> None:
        # [name=...] / [data-testid=...] etc. are stable identity anchors — kept verbatim.
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector='[data-testid="add-to-cart"]',
                    source_url="https://example.com/product",
                    role="button",
                    accessible_name="Add to cart",
                )
            ]
        )
        assert result is not None
        assert 'await page.locator("[data-testid=\\"add-to-cart\\"]").click()' in result.code
        assert "get_by_role" not in result.code

    def test_positional_nth_of_type_selector_uses_get_by_role_fallback(self) -> None:
        # A positional CSS path is fragile; when a role/name anchor exists, prefer it.
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="div.list > button:nth-of-type(3)",
                    source_url="https://example.com/list",
                    role="button",
                    accessible_name="More",
                )
            ]
        )
        assert result is not None
        assert 'await page.get_by_role("button", name="More").click()' in result.code

    def test_nth_engine_chain_uses_get_by_role_anchor(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector='role=button[name="More"] >> nth=2',
                    source_url="https://example.com/list",
                    role="button",
                    accessible_name="More",
                )
            ]
        )
        assert result is not None
        assert 'await page.get_by_role("button", name="More").click()' in result.code

    def test_positional_selector_without_role_name_keeps_selector_with_note(self) -> None:
        result = synthesize_code_block(
            [_interaction("click", selector="ul > li:nth-child(2)", source_url="https://example.com/results")]
        )
        assert result is not None
        assert 'await page.locator("ul > li:nth-child(2)").click()' in result.code
        assert any("low-confidence" in note for note in result.notes)

    def test_stable_bare_css_without_role_name_is_emitted_verbatim_no_note(self) -> None:
        result = synthesize_code_block(
            [_interaction("click", selector=".results .item", source_url="https://example.com/results")]
        )
        assert result is not None
        assert 'await page.locator(".results .item").click()' in result.code
        assert not any("low-confidence" in note for note in result.notes)

    def test_never_emits_first_or_last_callables(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector='role=link[name="Next"] >> nth=0',
                    source_url="https://example.com/",
                )
            ]
        )
        assert result is not None
        assert ".first" not in result.code
        assert ".last" not in result.code


class TestActionSynthesis:
    def test_type_text_becomes_param_slot_fill(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector='role=textbox[name="Search"]',
                    source_url="https://example.com/",
                    typed_length=11,
                    role="textbox",
                    accessible_name="Search",
                )
            ]
        )
        assert result is not None
        assert 'await page.get_by_role("textbox", name="Search").fill(str(search))' in result.code
        assert result.parameters == [{"key": "search"}]
        # Raw typed value is never captured.
        assert "value" not in result.code

    def test_type_text_defaults_are_private_reused_only_for_same_field_identity(self) -> None:
        def typed(selector: str, name: str, url: str = "https://example.com/") -> dict[str, Any]:
            return _interaction(
                "type_text",
                selector=selector,
                source_url=url,
                typed_length=15,
                typed_value="example_sku_123",
                role="textbox",
                accessible_name=name,
            )

        safe = synthesize_code_block([typed('role=textbox[name="Search"]', "Search")])
        assert safe is not None
        assert 'await page.get_by_role("textbox", name="Search").fill(str(search))' in safe.code
        assert "example_sku_123" not in safe.code
        assert safe.parameters == [{"key": "search", "default_value": "example_sku_123"}]

        offer_text = render_synthesized_offer_text(safe)
        assert "workflow_parameter_type: string" in offer_text
        assert "default_value" in offer_text
        assert "example_sku_123" not in offer_text

        reused = synthesize_code_block(
            [typed("#search", "Search"), typed("#search", "Search", "https://example.com/results")]
        )
        assert reused is not None
        assert reused.parameters == [{"key": "search", "default_value": "example_sku_123"}]
        assert reused.code.count("fill(str(search))") == 2

        distinct = synthesize_code_block([typed("#part-number", "Part Number"), typed("#coupon", "Coupon Code")])
        assert distinct is not None
        assert distinct.parameters == [
            {"key": "part_number", "default_value": "example_sku_123"},
            {"key": "coupon_code", "default_value": "example_sku_123"},
        ]

    def test_goto_entry_url_and_load_state(self) -> None:
        result = synthesize_code_block([_interaction("click", selector="#go", source_url="https://example.com/start")])
        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == '    await page.goto("https://example.com/start")'
        assert lines[1] == '    await page.wait_for_load_state("load")'

    def test_press_enter_uses_keyboard_when_no_selector(self) -> None:
        result = synthesize_code_block([_interaction("press_key", key="Enter")])
        assert result is not None
        assert 'await page.keyboard.press("Enter")' in result.code

    def test_press_key_on_located_element(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "press_key",
                    selector='role=textbox[name="Search"]',
                    key="Enter",
                    role="textbox",
                    accessible_name="Search",
                )
            ]
        )
        assert result is not None
        assert 'await page.get_by_role("textbox", name="Search").press("Enter")' in result.code

    def test_select_option_emits_value(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "select_option",
                    selector='role=combobox[name="Size"]',
                    source_url="https://example.com/",
                    value="large",
                    role="combobox",
                    accessible_name="Size",
                )
            ]
        )
        assert result is not None
        assert 'await page.get_by_role("combobox", name="Size").select_option("large")' in result.code

    def test_select_option_without_value_is_dropped_with_note(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "select_option",
                    selector='role=combobox[name="Size"]',
                    source_url="https://example.com/",
                    role="combobox",
                    accessible_name="Size",
                )
            ]
        )
        assert result is None or "select_option" not in result.code
        if result is not None:
            assert any("select_option" in note for note in result.notes)


class TestParamKeySafety:
    @staticmethod
    def _emitted_wrapper(code: str, param_keys: list[str]) -> str:
        # Mirror block.py generate_async_user_function: the param keys become the wrapper signature.
        signature = ", ".join(f"{key}=None" for key in param_keys)
        body = "\n".join(f"    {line.strip()}" for line in code.splitlines())
        return f"async def wrapper({signature}):\n{body or '    pass'}"

    def test_keyword_accessible_name_yields_bindable_identifier(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector='role=textbox[name="Class"]',
                    source_url="https://example.com/",
                    typed_length=4,
                    role="textbox",
                    accessible_name="Class",
                )
            ]
        )
        assert result is not None
        keys = [p["key"] for p in result.parameters]
        assert keys == ["class_field"]
        assert all(key.isidentifier() and not keyword.iskeyword(key) for key in keys)
        ast.parse(self._emitted_wrapper(result.code, keys))

    def test_reserved_safe_var_name_is_suffixed(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector='role=textbox[name="Page"]',
                    source_url="https://example.com/",
                    typed_length=4,
                    role="textbox",
                    accessible_name="Page",
                )
            ]
        )
        assert result is not None
        assert result.parameters == [{"key": "page_field"}]
        assert "fill(str(page))" not in result.code
        assert "fill(str(page_field))" in result.code

    def test_leading_digit_name_is_valid_identifier(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector='role=textbox[name="2nd line"]',
                    source_url="https://example.com/",
                    typed_length=4,
                    role="textbox",
                    accessible_name="2nd line",
                )
            ]
        )
        assert result is not None
        keys = [p["key"] for p in result.parameters]
        assert keys and all(key.isidentifier() for key in keys)
        ast.parse(self._emitted_wrapper(result.code, keys))


class TestTrajectoryFidelity:
    def test_two_same_selector_clicks_both_emitted(self) -> None:
        # Regression: scout_trajectory is append-only/non-deduped, so a repeated
        # click on the same selector must produce two clicks (the deduped list would drop one).
        trajectory = [
            _interaction(
                "click",
                selector='role=button[name="Add to cart"]',
                source_url="https://example.com/p",
                role="button",
                accessible_name="Add to cart",
            ),
            _interaction(
                "click",
                selector='role=button[name="Add to cart"]',
                source_url="https://example.com/p",
                role="button",
                accessible_name="Add to cart",
            ),
        ]
        result = synthesize_code_block(trajectory)
        assert result is not None
        assert result.code.count('await page.get_by_role("button", name="Add to cart").click()') == 2

    def test_same_name_disambiguation_keeps_distinct_param_keys(self) -> None:
        trajectory = [
            _interaction(
                "type_text",
                selector='role=textbox[name="Name"]',
                source_url="https://example.com/",
                typed_length=3,
                role="textbox",
                accessible_name="Name",
            ),
            _interaction(
                "type_text",
                selector='role=textbox[name="Name"]',
                source_url="https://example.com/",
                typed_length=3,
                role="textbox",
                accessible_name="Name",
            ),
        ]
        result = synthesize_code_block(trajectory)
        assert result is not None
        assert result.parameters == [{"key": "name"}, {"key": "name_2"}]
        assert "fill(str(name))" in result.code
        assert "fill(str(name_2))" in result.code

    def test_param_keys_are_globally_unique_against_external_slug_collision(self) -> None:
        # An externally-derived slug "name 2" produces base "name_2", which must not collide with the
        # auto-suffix of two "name" fields. All keys are tracked in one global used-set.
        trajectory = [
            _interaction(
                "type_text",
                selector="#a",
                source_url="https://example.com/",
                typed_length=3,
                role="textbox",
                accessible_name="Name",
            ),
            _interaction(
                "type_text",
                selector="#b",
                source_url="https://example.com/",
                typed_length=3,
                role="textbox",
                accessible_name="name 2",
            ),
            _interaction(
                "type_text",
                selector="#c",
                source_url="https://example.com/",
                typed_length=3,
                role="textbox",
                accessible_name="Name",
            ),
        ]
        result = synthesize_code_block(trajectory)
        assert result is not None
        keys = [p["key"] for p in result.parameters]
        assert keys == ["name", "name_2", "name_3"]
        assert len(set(keys)) == len(keys)
        for key in keys:
            assert f"fill(str({key}))" in result.code

    def test_step_cap_truncates_at_configured_limit(self) -> None:
        trajectory = [
            _interaction("click", selector=f'role=button[name="b{i}"]', source_url="https://example.com/")
            for i in range(_MAX_STEPS + 5)
        ]
        result = synthesize_code_block(trajectory)
        assert result is not None
        assert result.code.count(".click()") == _MAX_STEPS
        assert result.diagnostics.truncated is True
        assert any("truncated" in note for note in result.notes)

    def test_strict_synthesis_emits_byte_equal_selector_provenance(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector="#provInput",
                    source_url="https://example.com/find-care",
                    typed_length=13,
                    role="textbox",
                    accessible_name="Provider Name",
                )
            ],
            strict_selectors=True,
        )

        assert result is not None
        assert 'await page.locator("#provInput").fill(str(provider_name))' in result.code
        assert result.diagnostics.dropped_interactions == []
        assert result.diagnostics.locator_provenance == [
            {
                "trajectory_index": 0,
                "selector": "#provInput",
                "emitted_literal": "#provInput",
                "source": "selector",
            }
        ]

    def test_strict_synthesis_reports_unsupported_interaction(self) -> None:
        result = synthesize_code_block(
            [
                _interaction("click", selector="#open", source_url="https://example.com/"),
                _interaction("hover", selector="#menu", source_url="https://example.com/"),
            ],
            strict_selectors=True,
        )

        assert result is not None
        assert result.diagnostics.dropped_interactions == [
            {"trajectory_index": 1, "tool_name": "hover", "reason_code": "unsupported_tool"}
        ]

    def test_synthesis_scrubs_credentials_from_emitted_url_literals(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="#go",
                    source_url="https://user:password@example.com/search?token=secret-token&q=provider#access_token=fragment-token&section=results",
                )
            ]
        )
        metadata = build_synthesized_artifact_metadata(
            [
                _interaction(
                    "click",
                    selector="#go",
                    source_url="https://user:password@example.com/search?token=secret-token&q=provider#access_token=fragment-token&section=results",
                )
            ]
        )

        assert result is not None
        assert "user:password" not in result.code
        assert "secret-token" not in result.code
        assert "fragment-token" not in result.code
        assert "q=provider" in result.code
        assert "section=results" in result.code
        page_dependency = metadata["page_dependencies"][0]
        assert page_dependency["url_hint"] == (
            "https://example.com/search?token=__redacted__&q=provider#access_token=__redacted__&section=results"
        )

    def test_synthesis_scrubs_bare_sensitive_url_fragments(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="#go",
                    source_url="https://example.com/search?q=provider#secret-token-fragment",
                )
            ]
        )
        metadata = build_synthesized_artifact_metadata(
            [
                _interaction(
                    "click",
                    selector="#go",
                    source_url="https://example.com/search?q=provider#secret-token-fragment",
                )
            ]
        )

        assert result is not None
        assert "secret-token-fragment" not in result.code
        assert 'await page.goto("https://example.com/search?q=provider#__redacted__")' in result.code
        assert metadata["page_dependencies"][0]["url_hint"] == "https://example.com/search?q=provider#__redacted__"


class TestDeterminismAndEmpty:
    def test_empty_trajectory_returns_none(self) -> None:
        assert synthesize_code_block([]) is None

    def test_byte_identical_per_trajectory(self) -> None:
        trajectory = [
            _interaction(
                "type_text",
                selector='role=textbox[name="Search"]',
                source_url="https://example.com/",
                typed_length=4,
                role="textbox",
                accessible_name="Search",
            ),
            _interaction("press_key", key="Enter"),
            _interaction(
                "click",
                selector='role=button[name="Go"]',
                role="button",
                accessible_name="Go",
            ),
        ]
        first = synthesize_code_block(trajectory)
        second = synthesize_code_block(trajectory)
        assert first is not None and second is not None
        assert first.code == second.code
        assert first.parameters == second.parameters
        assert first.notes == second.notes


class TestLineBoundaryEscaping:
    # str.splitlines() and several parsers treat each of these as a line boundary. An attacker-controlled
    # page can plant one in an accessible name or option value; left unescaped it splits the emitted
    # one-line literal across lines and corrupts the block (availability, not RCE — the leading quote
    # precedes the payload and every attacker quote is escaped).
    _BOUNDARY_CODEPOINTS = ("\x0b", "\x0c", "\x85", " ", " ")

    @staticmethod
    def _parses(code: str) -> ast.Module:
        wrapper = "async def __wrapper__(payload=None):\n" + "\n".join(
            f"    {line.strip()}" for line in code.splitlines()
        )
        return ast.parse(wrapper)

    def test_accessible_name_boundary_codepoints_keep_block_parseable(self) -> None:
        for codepoint in self._BOUNDARY_CODEPOINTS:
            name = f"Search{codepoint}payload"
            result = synthesize_code_block(
                [
                    _interaction(
                        "click",
                        selector=f'role=button[name="{name}"]',
                        source_url="https://example.com/",
                        role="button",
                        accessible_name=name,
                    )
                ]
            )
            assert result is not None, f"codepoint U+{ord(codepoint):04X} produced no block"
            # The block parses with no SyntaxError despite the raw line boundary in the name.
            self._parses(result.code)
            # The raw codepoint never reaches the emitted source; it survives only as a backslash escape,
            # so the payload stays inert inside the single-line literal.
            assert codepoint not in result.code, f"raw U+{ord(codepoint):04X} leaked into emitted code"
            assert "payload" in result.code

    def test_select_option_value_boundary_codepoint_keeps_block_parseable(self) -> None:
        value = "small\x0bvalue"
        result = synthesize_code_block(
            [
                _interaction(
                    "select_option",
                    selector='role=combobox[name="Size"]',
                    source_url="https://example.com/",
                    value=value,
                    role="combobox",
                    accessible_name="Size",
                )
            ]
        )
        assert result is not None
        self._parses(result.code)
        assert "\x0b" not in result.code
        assert "select_option" in result.code

    def test_c0_control_codepoint_in_name_is_escaped(self) -> None:
        # Belt-and-suspenders: the defensive C0/C1 control pass also escapes non-line-boundary controls.
        name = "Search\x07bell"
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector=f'role=button[name="{name}"]',
                    source_url="https://example.com/",
                    role="button",
                    accessible_name=name,
                )
            ]
        )
        assert result is not None
        self._parses(result.code)
        assert "\x07" not in result.code


class TestPreflightSurfacesSyntaxError:
    def test_synthesized_block_round_trips_through_preflight(self) -> None:
        # A well-formed synthesized block (boundary codepoint escaped) yields no syntax diagnostic.
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector='role=button[name="Go\x0bx"]',
                    source_url="https://example.com/",
                    role="button",
                    accessible_name="Go\x0bx",
                )
            ]
        )
        assert result is not None
        diagnostics = preflight_code_block(result.code, parameter_keys=())
        assert not any(d.code == "SYNTAX_ERROR" for d in diagnostics)

    def test_unparseable_block_surfaces_syntax_diagnostic(self) -> None:
        # A malformed block must be caught at authoring time, not swallowed into a silent run-time failure.
        diagnostics = preflight_code_block('await page.goto("unterminated)\n', parameter_keys=())
        assert any(d.code == "SYNTAX_ERROR" for d in diagnostics)


class TestRenderSynthesizedOfferText:
    def test_renders_label_code_and_params(self) -> None:
        synthesized = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector="#searcher_s",
                    source_url="https://example.com/",
                    typed_length=5,
                    role="textbox",
                    accessible_name="Search",
                )
            ]
        )
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized)
        assert text.startswith("SYNTHESIZED CODE BLOCK (offered once).")
        assert _SYNTHESIZED_BLOCK_LABEL in text
        assert "```python" in text
        assert 'await page.locator("#searcher_s").fill(str(search))' in text
        assert "Workflow parameters referenced (bind these): search." in text

    def test_omits_param_line_when_no_parameters(self) -> None:
        synthesized = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector='role=button[name="Go"]',
                    source_url="https://example.com/",
                    role="button",
                    accessible_name="Go",
                )
            ]
        )
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized)
        assert "Workflow parameters referenced" not in text

    def test_includes_synthesis_notes_when_present(self) -> None:
        synthesized = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="li:nth-of-type(3) > a",
                    source_url="https://example.com/",
                )
            ]
        )
        assert synthesized is not None
        assert synthesized.notes
        text = render_synthesized_offer_text(synthesized)
        assert "Synthesis notes: " in text


def _code_block_yaml(label: str) -> str:
    return (
        "workflow_definition:\n"
        "  blocks:\n"
        "    - block_type: code\n"
        f"      label: {label}\n"
        "      code: |\n"
        '        await page.goto("https://example.com/")\n'
    )


_SCOUT_TRAJECTORY = [
    {
        "tool_name": "type_text",
        "selector": "#search-box",
        "source_url": "https://example.com/",
        "typed_length": 5,
        "role": "textbox",
        "accessible_name": "Search",
    },
    {"tool_name": "press_key", "selector": "#search-box", "key": "Enter"},
    {
        "tool_name": "click",
        "selector": 'role=button[name="Add to cart"]',
        "role": "button",
        "accessible_name": "Add to cart",
    },
]


class TestSynthesizedArtifactMetadata:
    def test_skeleton_passes_the_validator_with_placeholders(self) -> None:
        # The skeleton passes the validator with only <fill> placeholders for the model-owned slots.
        metadata = build_synthesized_artifact_metadata(_SCOUT_TRAJECTORY)
        normalized, error = _normalize_code_artifact_metadata([metadata], _code_block_yaml(_SYNTHESIZED_BLOCK_LABEL))
        assert error is None
        assert list(normalized.keys()) == [_SYNTHESIZED_BLOCK_LABEL]

    def test_skeleton_never_asserts_satisfied_status(self) -> None:
        # The scout never ran+verified the authored block, so the only honest status is observed_not_verified.
        metadata = build_synthesized_artifact_metadata(_SCOUT_TRAJECTORY)
        statuses = [metadata["page_dependencies"][0]["status"], metadata["observation_refs"][0]["status"]]
        statuses += [claim["status"] for claim in metadata["claimed_outcomes"]]
        assert all(status == "observed_not_verified" for status in statuses)
        assert "satisfied" not in str(metadata)

    def test_skeleton_observation_ref_carries_scout_source_tool(self) -> None:
        metadata = build_synthesized_artifact_metadata(_SCOUT_TRAJECTORY)
        observation_ref = metadata["observation_refs"][0]
        assert observation_ref["source_tool"] == "scout_interaction"
        assert observation_ref["dependency_id"] == metadata["page_dependencies"][0]["id"]

    def test_skeleton_leaves_terminal_goal_for_the_model(self) -> None:
        metadata = build_synthesized_artifact_metadata(_SCOUT_TRAJECTORY)
        assert metadata["declared_goal"].startswith("<fill:")
        assert metadata["completion_criteria"][0]["text"].startswith("<fill:")
        assert metadata["claimed_outcomes"][0]["text"].startswith("<fill:")

    def test_skeleton_is_byte_identical_per_trajectory(self) -> None:
        import json

        first = build_synthesized_artifact_metadata(_SCOUT_TRAJECTORY)
        second = build_synthesized_artifact_metadata(_SCOUT_TRAJECTORY)
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_skeleton_omits_url_hint_when_no_source_url(self) -> None:
        metadata = build_synthesized_artifact_metadata([_interaction("press_key", key="Enter")])
        assert "url_hint" not in metadata["page_dependencies"][0]
        assert "current_url" not in metadata["observation_refs"][0]

    def test_offer_text_embeds_metadata_when_trajectory_supplied(self) -> None:
        synthesized = synthesize_code_block(_SCOUT_TRAJECTORY)
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized, _SCOUT_TRAJECTORY)
        assert "code_artifact_metadata" in text
        assert "```json" in text
        assert "scout_interaction" in text
        assert "returns every remaining violation at once" in text

    def test_offer_text_omits_metadata_without_trajectory(self) -> None:
        synthesized = synthesize_code_block(_SCOUT_TRAJECTORY)
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized)
        assert "code_artifact_metadata" not in text
        assert "```json" not in text


class TestCredentialFillSynthesis:
    """A scouted fill_credential_field compiles into an attribute read on a
    credential-bound parameter — references only, never values."""

    def _credential_fill(self, **overrides: Any) -> dict[str, Any]:
        fields = {
            "selector": "#userName",
            "source_url": "https://authenticationtest.com/simpleFormAuth/",
            "typed_length": 24,
            "credential_id": "cred_123",
            "credential_field": "username",
            "credential_name": "authtest simple",
        }
        fields.update(overrides)
        return _interaction("fill_credential_field", **fields)

    def test_emits_attribute_fill_and_credential_parameter(self) -> None:
        result = synthesize_code_block([self._credential_fill()])
        assert result is not None
        assert 'await page.locator("#userName").fill(authtest_simple.username)' in result.code
        assert result.parameters == [{"key": "authtest_simple", "credential_id": "cred_123"}]

    def test_same_credential_shares_one_parameter(self) -> None:
        result = synthesize_code_block(
            [
                self._credential_fill(),
                self._credential_fill(selector="#passwordInput", credential_field="password", typed_length=12),
            ]
        )
        assert result is not None
        assert 'await page.locator("#userName").fill(authtest_simple.username)' in result.code
        assert 'await page.locator("#passwordInput").fill(authtest_simple.password)' in result.code
        assert result.parameters == [{"key": "authtest_simple", "credential_id": "cred_123"}]

    def test_totp_field_reads_totp_attribute(self) -> None:
        result = synthesize_code_block(
            [self._credential_fill(selector="#totpCode", credential_field="totp", typed_length=6)]
        )
        assert result is not None
        assert 'await page.locator("#totpCode").fill(authtest_simple.totp)' in result.code

    def test_missing_credential_reference_is_dropped_with_note(self) -> None:
        result = synthesize_code_block(
            [
                self._credential_fill(credential_id=""),
                _interaction("click", selector="#next", source_url="https://example.com/login"),
            ]
        )
        assert result is not None
        assert ".fill(" not in result.code
        assert result.parameters == []
        assert any("credential" in note for note in result.notes)

    def test_unknown_credential_field_is_dropped(self) -> None:
        result = synthesize_code_block(
            [
                self._credential_fill(credential_field="cvv"),
                _interaction("click", selector="#next", source_url="https://example.com/login"),
            ]
        )
        assert result is not None
        assert ".fill(" not in result.code
        assert result.parameters == []

    def test_param_key_defaults_when_credential_name_missing(self) -> None:
        result = synthesize_code_block([self._credential_fill(credential_name="")])
        assert result is not None
        assert ".fill(credential.username)" in result.code
        assert result.parameters == [{"key": "credential", "credential_id": "cred_123"}]

    def test_credential_param_key_does_not_collide_with_typed_param(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector="#company",
                    source_url="https://example.com/form",
                    typed_length=6,
                    role="textbox",
                    accessible_name="authtest simple",
                ),
                self._credential_fill(),
            ]
        )
        assert result is not None
        assert result.parameters[0] == {"key": "authtest_simple"}
        assert result.parameters[1] == {"key": "authtest_simple_2", "credential_id": "cred_123"}
        assert ".fill(authtest_simple_2.username)" in result.code

    def test_offer_text_carries_credential_binding_contract(self) -> None:
        trajectory = [self._credential_fill()]
        synthesized = synthesize_code_block(trajectory)
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized, trajectory)
        assert "`authtest_simple` -> `cred_123`" in text
        assert "workflow_parameter_type: credential_id" in text
        assert "default_value" in text
        assert ".username` / `.password` / `.totp`" in text
        assert "authtest_simple" not in [p.get("key") for p in synthesized.parameters if "credential_id" not in p]

    def test_credential_parameters_excluded_from_plain_bind_line(self) -> None:
        trajectory = [
            _interaction(
                "type_text",
                selector="#q",
                source_url="https://example.com/",
                typed_length=4,
                role="textbox",
                accessible_name="Search",
            ),
            self._credential_fill(),
        ]
        synthesized = synthesize_code_block(trajectory)
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized, trajectory)
        assert "Workflow parameters referenced (bind these): search." in text
        assert "Credential parameters referenced" in text

    def test_plain_param_never_takes_a_bare_credential_field_name(self) -> None:
        # CodeBlock.execute injects a bound credential's fields under the bare
        # names username/password/totp, so a plain typed parameter must not
        # claim those keys or it would resolve to the secret value at runtime.
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector="#confirm",
                    source_url="https://example.com/form",
                    typed_length=8,
                    role="textbox",
                    accessible_name="Password",
                ),
                self._credential_fill(),
            ]
        )
        assert result is not None
        assert result.parameters[0] == {"key": "password_field"}
        assert "fill(str(password_field))" in result.code
        assert {"key": "password"} not in result.parameters

    def test_synthesized_credential_code_is_valid_python(self) -> None:
        result = synthesize_code_block(
            [
                self._credential_fill(),
                self._credential_fill(selector="#passwordInput", credential_field="password"),
            ]
        )
        assert result is not None
        wrapped = "async def _block(page, authtest_simple):\n" + result.code
        ast.parse(wrapped)


def test_code_block_preflight_restores_recursion_limit() -> None:
    before = sys.getrecursionlimit()
    preflight_code_block("await page.locator('button[type=submit]').first.click(timeout=5000)\n")

    assert sys.getrecursionlimit() == before


class TestOfferDemonstratesStructuredReturn:
    def test_offer_directs_keyed_return_not_inner_text_blob(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="#search-submit",
                    source_url="https://example.com/search",
                )
            ]
        )
        assert result is not None
        offer = render_synthesized_offer_text(
            result,
            trajectory=[
                {"tool_name": "click", "selector": "#search-submit", "source_url": "https://example.com/search"}
            ],
        )
        assert "keyed structure" in offer
        assert "inner_text" in offer
        assert 'return {"records":' in offer
