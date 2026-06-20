"""Tests for the pure copilot code-block synthesizer.

OSS-synced: only example.* / RFC-2606 placeholder targets.
"""

from __future__ import annotations

import ast
import json
import keyword
import sys
from typing import Any

import pytest

from skyvern.forge.sdk.copilot.code_block_preflight import preflight_code_block
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    _DOWNLOAD_VAR_BASE,
    _MAX_STEPS,
    _SYNTHESIZED_BLOCK_LABEL,
    CREDENTIAL_FILL_TOOL_NAME,
    build_synthesized_artifact_metadata,
    code_contains_credential_fill,
    is_optional_dismissal_only_trajectory,
    render_synthesized_offer_text,
    synthesize_code_block,
)
from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget
from skyvern.forge.sdk.copilot.tools import _normalize_code_artifact_metadata
from skyvern.forge.sdk.copilot.tools.workflow_update import _code_block_safety_errors
from skyvern.forge.sdk.workflow.models.block import CodeBlock, CodeBlockStep


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

    def test_positional_role_name_anchor_does_not_emit_first(self) -> None:
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

    def test_bare_tag_selector_disambiguated_to_first(self) -> None:
        result = synthesize_code_block(
            [_interaction("click", selector="button", source_url="https://example.com/login")]
        )
        assert result is not None
        assert 'await page.locator("button").first.click()' in result.code
        assert any("disambiguated a bare" in note for note in result.notes)
        assert any(p.get("source") == "first_fallback" for p in result.diagnostics.locator_provenance)

    def test_bare_role_no_name_disambiguated_to_first(self) -> None:
        result = synthesize_code_block(
            [_interaction("click", selector="role=button", source_url="https://example.com/login")]
        )
        assert result is not None
        assert 'await page.get_by_role("button").first.click()' in result.code

    def test_bare_selector_with_role_name_anchors_on_get_by_role(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="button",
                    source_url="https://example.com/login",
                    role="button",
                    accessible_name="Continue",
                )
            ]
        )
        assert result is not None
        assert 'await page.get_by_role("button", name="Continue").click()' in result.code
        assert ".first" not in result.code

    def test_stable_selector_not_disambiguated_to_first(self) -> None:
        for selector in ("#submit", '[name="email"]', '[data-testid="go"]', ".results .item"):
            result = synthesize_code_block(
                [_interaction("click", selector=selector, source_url="https://example.com/")]
            )
            assert result is not None
            assert ".first" not in result.code, selector

    def test_strict_imposed_refuses_ambiguous_bare_selector(self) -> None:
        trajectory = [
            _interaction("click", selector="#open-login", source_url="https://example.com/home"),
            _interaction("click", selector="button", source_url="https://example.com/login"),
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert ".first" not in result.code
        assert 'await page.locator("button").click()' not in result.code
        dropped = [
            d for d in result.diagnostics.dropped_interactions if d.get("reason_code") == "ambiguous_bare_selector"
        ]
        assert dropped

    def test_strict_imposed_refuses_bare_role_no_name(self) -> None:
        trajectory = [
            _interaction("click", selector="#open-login", source_url="https://example.com/home"),
            _interaction("click", selector="role=button", source_url="https://example.com/login"),
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert ".first" not in result.code
        dropped = [
            d for d in result.diagnostics.dropped_interactions if d.get("reason_code") == "ambiguous_bare_selector"
        ]
        assert dropped

    def test_two_bare_button_login_first_clicks_both_emit_first(self) -> None:
        trajectory = [
            _interaction("click", selector="button", source_url="https://example.com/login"),
            _interaction("click", selector="button", source_url="https://example.com/login"),
        ]
        result = synthesize_code_block(trajectory)
        assert result is not None
        assert result.code.count('await page.locator("button").first.click()') == 2
        ast.parse("async def _block(page):\n" + result.code)

    def test_universal_selector_offered_with_first(self) -> None:
        result = synthesize_code_block([_interaction("click", selector="*", source_url="https://example.com/p")])
        assert result is not None
        assert 'await page.locator("*").first.click()' in result.code
        assert 'await page.locator("*").click()' not in result.code

    def test_strict_imposed_refuses_universal_selector(self) -> None:
        trajectory = [
            _interaction("click", selector="#open", source_url="https://example.com/home"),
            _interaction("click", selector="*", source_url="https://example.com/p"),
        ]
        result = synthesize_code_block(trajectory, strict_selectors=True)
        assert result is not None
        assert 'page.locator("*")' not in result.code
        assert [d for d in result.diagnostics.dropped_interactions if d.get("reason_code") == "ambiguous_bare_selector"]

    def test_attribute_qualified_universal_selector_not_disambiguated(self) -> None:
        result = synthesize_code_block(
            [_interaction("click", selector="*[data-id]", source_url="https://example.com/p")]
        )
        assert result is not None
        assert 'await page.locator("*[data-id]").click()' in result.code
        assert ".first" not in result.code


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

    def test_strict_type_text_carries_typed_length_without_value(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector="#locInput",
                    source_url="https://example.com/",
                    typed_length=19,
                    role="textbox",
                    accessible_name="Address or postal code",
                )
            ],
            strict_selectors=True,
        )

        assert result is not None
        assert result.parameters == [{"key": "address_or_postal_code", "typed_length": "19"}]
        assert "Example City" not in result.code

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

    def test_entry_url_is_selector_gated_and_uses_domcontentloaded(self) -> None:
        result = synthesize_code_block([_interaction("click", selector="#go", source_url="https://example.com/start")])
        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == '    _scout_entry_target = page.locator("#go")'
        assert lines[1] == "    try:"
        assert lines[2] == '        await _scout_entry_target.wait_for(state="visible", timeout=1000)'
        assert lines[3] == "    except Exception:"
        assert lines[4] == '        await page.goto("https://example.com/start", wait_until="domcontentloaded")'
        assert lines[5] == '        await _scout_entry_target.wait_for(state="visible")'
        assert "        del _scout_entry_target" in lines

    def test_optional_cookie_dismissal_is_conditional_and_uses_durable_entry_target(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="#accept-consent",
                    source_url="https://example.com/find",
                    role="button",
                    accessible_name="Accept cookies",
                ),
                _interaction(
                    "type_text",
                    selector="#locInput",
                    source_url="https://example.com/find",
                    role="textbox",
                    accessible_name="City, county, or ZIP code",
                    typed_value="Example City",
                ),
            ]
        )
        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == '    _scout_entry_target = page.locator("#locInput")'
        assert '        await page.goto("https://example.com/find", wait_until="domcontentloaded")' in lines
        assert '        await _scout_entry_target.wait_for(state="visible")' in lines
        assert '    _scout_optional_dismissal = page.locator("#accept-consent")' in lines
        assert "    if await _scout_optional_dismissal.count() > 0:" in lines
        assert "            await _scout_optional_dismissal.first.click(timeout=1000)" in lines
        assert result.code.index("_scout_optional_dismissal") < result.code.index(
            'await page.locator("#locInput").fill'
        )
        assert "        del _scout_entry_target" in lines
        assert "        del _scout_optional_dismissal" in lines
        assert result.parameters == [{"key": "city_county_or_zip_code", "default_value": "Example City"}]
        ast.parse("async def _block(page):\n" + result.code)

    def test_optional_cookie_decline_is_conditional_and_not_entry_target(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="button.decline",
                    source_url="https://example.com/find",
                    role="button",
                    accessible_name="Decline cookies",
                ),
                _interaction(
                    "type_text",
                    selector="#locInput",
                    source_url="https://example.com/find",
                    role="textbox",
                    accessible_name="City, county, or ZIP code",
                    typed_value="Example City",
                ),
            ],
            strict_selectors=True,
        )

        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == '    _scout_entry_target = page.locator("#locInput")'
        assert '    _scout_optional_dismissal = page.locator("button.decline")' in lines
        assert "    if await _scout_optional_dismissal.count() > 0:" in lines
        assert 'await _scout_entry_target.wait_for(state="visible")' in result.code
        assert 'await page.locator("button.decline").click()' not in result.code

    def test_close_named_action_is_not_optional_dismissal_by_name_only(self) -> None:
        trajectory = [
            _interaction(
                "click",
                selector="#account-action",
                source_url="https://example.com/settings",
                role="button",
                accessible_name="Close account",
            )
        ]

        assert is_optional_dismissal_only_trajectory(trajectory) is False

        result = synthesize_code_block(trajectory, strict_selectors=True)

        assert result is not None
        assert 'await page.locator("#account-action").click()' in result.code
        assert "_scout_optional_dismissal" not in result.code

    def test_internal_scout_cleanup_ignores_names_inside_literals(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="#start",
                    source_url="https://example.com/_scout_optional_dismissal",
                )
            ]
        )

        assert result is not None
        assert 'await page.goto("https://example.com/_scout_optional_dismissal"' in result.code
        assert "        del _scout_entry_target" in result.code
        assert "        del _scout_optional_dismissal" not in result.code

    def test_structural_cookie_button_is_conditional_when_durable_target_follows(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector=".btns button:nth-of-type(2)",
                    source_url="https://example.com/find",
                    role="button",
                ),
                _interaction(
                    "type_text",
                    selector="#npiInput",
                    source_url="https://example.com/find",
                    role="textbox",
                    accessible_name="Provider ID",
                    typed_value="ID-12345",
                ),
            ],
            strict_selectors=True,
        )

        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == '    _scout_entry_target = page.locator("#npiInput")'
        assert '    _scout_optional_dismissal = page.locator(".btns button:nth-of-type(2)")' in lines
        assert "            await _scout_optional_dismissal.first.click(timeout=1000)" in lines
        assert 'await page.locator(".btns button:nth-of-type(2)").click()' not in result.code
        assert result.code.index("_scout_optional_dismissal") < result.code.index(
            'await page.locator("#npiInput").fill'
        )

    def test_not_decline_cookie_button_is_conditional_when_durable_target_follows(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="button:not(.decline):nth-of-type(6)",
                    source_url="https://example.com/find",
                    role="button",
                ),
                _interaction(
                    "type_text",
                    selector="#locInput",
                    source_url="https://example.com/find",
                    role="textbox",
                    accessible_name="City, county, or ZIP code",
                    typed_value="Example City",
                ),
            ],
            strict_selectors=True,
        )

        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == '    _scout_entry_target = page.locator("#locInput")'
        assert "    _scout_optional_dismissal = page.locator(\"button:has-text('Accept')\")" in lines
        assert "            await _scout_optional_dismissal.first.click(timeout=1000)" in lines
        assert 'await page.locator("button:not(.decline):nth-of-type(6)").click()' not in result.code

    def test_cookie_accept_xpath_is_conditional_when_durable_target_follows(self) -> None:
        cookie_accept_xpath = (
            'xpath=/*[name()="html"][1]/*[name()="body"][1]/*[name()="div"][1]'
            '/*[name()="div"][2]/*[name()="div"][1]/*[name()="button"][2]'
        )

        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector=cookie_accept_xpath,
                    source_url="https://example.com/find",
                    role="button",
                ),
                _interaction(
                    "type_text",
                    selector="#locInput",
                    source_url="https://example.com/find",
                    role="textbox",
                    accessible_name="City, county, or ZIP code",
                    typed_value="Example City",
                ),
            ],
            strict_selectors=True,
        )

        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == '    _scout_entry_target = page.locator("#locInput")'
        assert "    _scout_optional_dismissal = page.locator(\"button:has-text('Accept')\")" in lines
        assert "            await _scout_optional_dismissal.first.click(timeout=1000)" in lines
        assert cookie_accept_xpath not in result.code
        assert result.code.index("_scout_optional_dismissal") < result.code.index(
            'await page.locator("#locInput").fill'
        )

    def test_normalized_accept_xpath_is_conditional_when_durable_target_follows(self) -> None:
        accept_xpath = "xpath=//button[normalize-space()='Accept']"

        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector=accept_xpath,
                    source_url="https://example.com/find",
                    role="button",
                ),
                _interaction(
                    "type_text",
                    selector="#locInput",
                    source_url="https://example.com/find",
                    role="textbox",
                    accessible_name="City, county, or ZIP code",
                    typed_value="Example City",
                ),
            ],
            strict_selectors=True,
        )

        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == '    _scout_entry_target = page.locator("#locInput")'
        assert "    _scout_optional_dismissal = page.locator(\"button:has-text('Accept')\")" in lines
        assert "            await _scout_optional_dismissal.first.click(timeout=1000)" in lines
        assert accept_xpath not in result.code

    def test_bare_normalized_accept_xpath_is_conditional_when_durable_target_follows(self) -> None:
        accept_xpath = "//button[normalize-space()='Accept']"

        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector=accept_xpath,
                    source_url="https://example.com/find",
                    role=None,
                ),
                _interaction(
                    "type_text",
                    selector="#locInput",
                    source_url="https://example.com/find",
                    role="textbox",
                    accessible_name="City, county, or ZIP code",
                    typed_value="Example City",
                ),
            ],
            strict_selectors=True,
        )

        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == '    _scout_entry_target = page.locator("#locInput")'
        assert "    _scout_optional_dismissal = page.locator(\"button:has-text('Accept')\")" in lines
        assert "            await _scout_optional_dismissal.first.click(timeout=1000)" in lines
        assert accept_xpath not in result.code

    def test_one_step_not_decline_cookie_button_is_not_entry_target(self) -> None:
        trajectory = [
            _interaction(
                "click",
                selector="button:not(.decline)",
                source_url="https://example.com/find",
                role="button",
            ),
        ]
        assert is_optional_dismissal_only_trajectory(trajectory) is True

        result = synthesize_code_block(trajectory, strict_selectors=True)

        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == '    await page.goto("https://example.com/find", wait_until="domcontentloaded")'
        assert "    _scout_optional_dismissal = page.locator(\"button:has-text('Accept')\")" in lines
        assert "            await _scout_optional_dismissal.first.click(timeout=1000)" in lines
        assert 'await _scout_entry_target.wait_for(state="visible")' not in result.code
        assert 'page.locator("button:not(.decline)")' not in result.code
        ast.parse("async def _block(page):\n" + result.code)

    def test_one_step_structural_cookie_button_is_not_entry_target(self) -> None:
        trajectory = [
            _interaction(
                "click",
                selector=".btns button:nth-of-type(2)",
                source_url="https://example.com/find",
                role="button",
            ),
        ]
        assert is_optional_dismissal_only_trajectory(trajectory) is True

        result = synthesize_code_block(trajectory, strict_selectors=True)

        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == '    await page.goto("https://example.com/find", wait_until="domcontentloaded")'
        assert '    _scout_optional_dismissal = page.locator(".btns button:nth-of-type(2)")' in lines
        assert "            await _scout_optional_dismissal.first.click(timeout=1000)" in lines
        assert 'await _scout_entry_target.wait_for(state="visible")' not in result.code
        assert 'await page.locator(".btns button:nth-of-type(2)").click()' not in result.code

    def test_one_step_bare_accept_xpath_is_not_entry_target(self) -> None:
        trajectory = [
            _interaction(
                "click",
                selector="//button[normalize-space()='Accept']",
                source_url="https://example.com/find",
                role=None,
            ),
        ]
        assert is_optional_dismissal_only_trajectory(trajectory) is True

        result = synthesize_code_block(trajectory, strict_selectors=True)

        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == '    await page.goto("https://example.com/find", wait_until="domcontentloaded")'
        assert "    _scout_optional_dismissal = page.locator(\"button:has-text('Accept')\")" in lines
        assert "            await _scout_optional_dismissal.first.click(timeout=1000)" in lines
        assert 'await _scout_entry_target.wait_for(state="visible")' not in result.code
        assert "//button[normalize-space()='Accept']" not in result.code

    def test_optional_dismissal_with_durable_target_is_offerable(self) -> None:
        trajectory = [
            _interaction(
                "click",
                selector="button:not(.decline)",
                source_url="https://example.com/find",
                role="button",
            ),
            _interaction(
                "type_text",
                selector="#locInput",
                source_url="https://example.com/find",
                role="textbox",
                accessible_name="City, county, or ZIP code",
                typed_value="Example City",
            ),
        ]

        assert is_optional_dismissal_only_trajectory(trajectory) is False

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
        body = "\n".join(f"    {line}" for line in code.splitlines())
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
                    selector="#searchInput",
                    source_url="https://example.com/find-care",
                    typed_length=13,
                    role="textbox",
                    accessible_name="Entity Name",
                )
            ],
            strict_selectors=True,
        )

        assert result is not None
        assert 'await page.locator("#searchInput").fill(str(entity_name))' in result.code
        assert result.diagnostics.dropped_interactions == []
        assert result.diagnostics.locator_provenance == [
            {
                "trajectory_index": 0,
                "selector": "#searchInput",
                "emitted_literal": "#searchInput",
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
                    source_url="https://user:password@example.com/search?token=secret-token&q=record#access_token=fragment-token&section=results",
                )
            ]
        )
        metadata = build_synthesized_artifact_metadata(
            [
                _interaction(
                    "click",
                    selector="#go",
                    source_url="https://user:password@example.com/search?token=secret-token&q=record#access_token=fragment-token&section=results",
                )
            ]
        )

        assert result is not None
        assert "user:password" not in result.code
        assert "secret-token" not in result.code
        assert "fragment-token" not in result.code
        assert "q=record" in result.code
        assert "section=results" in result.code
        page_dependency = metadata["page_dependencies"][0]
        assert page_dependency["url_hint"] == (
            "https://example.com/search?token=__redacted__&q=record#access_token=__redacted__&section=results"
        )

    def test_synthesis_scrubs_bare_sensitive_url_fragments(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="#go",
                    source_url="https://example.com/search?q=record#secret-token-fragment",
                )
            ]
        )
        metadata = build_synthesized_artifact_metadata(
            [
                _interaction(
                    "click",
                    selector="#go",
                    source_url="https://example.com/search?q=record#secret-token-fragment",
                )
            ]
        )

        assert result is not None
        assert "secret-token-fragment" not in result.code
        assert (
            'await page.goto("https://example.com/search?q=record#__redacted__", wait_until="domcontentloaded")'
            in result.code
        )
        assert metadata["page_dependencies"][0]["url_hint"] == "https://example.com/search?q=record#__redacted__"


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


class TestStepEmission:
    def test_synthesize_emits_goal_ready_steps(self) -> None:
        trajectory = [
            _interaction("click", selector="#go", source_url="https://example.com"),
            _interaction("type_text", selector="#q", typed_length=5),
        ]
        block = synthesize_code_block(trajectory)
        assert block is not None
        assert [s["action_type"] for s in block.steps] == ["goto_url", "click", "input_text"]
        assert all(s["description"] for s in block.steps)

    def test_step_line_spans_cover_every_emitted_line(self) -> None:
        trajectory = [
            _interaction("click", selector="#go", source_url="https://example.com/start"),
            _interaction(
                "type_text",
                selector="#q",
                typed_length=5,
                role="textbox",
                accessible_name="Search",
            ),
            _interaction("press_key", key="Enter"),
        ]
        block = synthesize_code_block(trajectory)
        assert block is not None
        code_lines = block.code.splitlines()
        goto_step, click_step, fill_step, key_step = block.steps
        assert (goto_step["line_start"], goto_step["line_end"]) == (1, 6)
        assert code_lines[goto_step["line_start"] - 1].lstrip().startswith("_scout_entry_target = ")
        assert (click_step["line_start"], click_step["line_end"]) == (7, 8)
        assert ".click()" in code_lines[click_step["line_start"] - 1]
        assert (fill_step["line_start"], fill_step["line_end"]) == (9, 9)
        assert ".fill(" in code_lines[fill_step["line_start"] - 1]
        assert key_step["action_type"] == "keypress"
        assert (key_step["line_start"], key_step["line_end"]) == (10, len(code_lines))
        assert "press" in code_lines[key_step["line_start"] - 1]
        # Spans are contiguous and cover the whole block.
        assert block.steps[0]["line_start"] == 1
        assert block.steps[-1]["line_end"] == len(code_lines)
        for previous, current in zip(block.steps, block.steps[1:]):
            assert current["line_start"] == previous["line_end"] + 1

    def test_select_option_and_press_key_action_types(self) -> None:
        trajectory = [
            _interaction(
                "select_option",
                selector='role=combobox[name="Size"]',
                source_url="https://example.com/",
                value="large",
                role="combobox",
                accessible_name="Size",
            ),
            _interaction("press_key", key="Enter"),
        ]
        block = synthesize_code_block(trajectory)
        assert block is not None
        assert [s["action_type"] for s in block.steps] == ["goto_url", "select_option", "keypress"]

    def test_skipped_interactions_emit_no_step(self) -> None:
        trajectory = [
            _interaction("click", selector="#go", source_url="https://example.com/"),
            _interaction("hover", selector="#menu"),
            _interaction("select_option", selector="#size"),
            _interaction("click"),
            _interaction("press_key", key=""),
        ]
        block = synthesize_code_block(trajectory)
        assert block is not None
        assert [s["action_type"] for s in block.steps] == ["goto_url", "click"]

    def test_no_entry_url_means_no_goto_step(self) -> None:
        block = synthesize_code_block([_interaction("press_key", key="Enter")])
        assert block is not None
        assert [s["action_type"] for s in block.steps] == ["keypress"]

    def test_step_descriptions_prefer_accessible_name_over_selector(self) -> None:
        block = synthesize_code_block(
            [
                _interaction(
                    "click",
                    selector="div.list > button:nth-of-type(3)",
                    source_url="https://example.com/",
                    role="button",
                    accessible_name="Add to cart",
                )
            ]
        )
        assert block is not None
        click_step = next(s for s in block.steps if s["action_type"] == "click")
        assert "Add to cart" in click_step["description"]
        assert "nth-of-type" not in click_step["description"]

    def test_entry_url_step_description_carries_url(self) -> None:
        block = synthesize_code_block([_interaction("click", selector="#go", source_url="https://example.com/start")])
        assert block is not None
        goto_step = block.steps[0]
        assert goto_step["action_type"] == "goto_url"
        assert "https://example.com/start" in goto_step["description"]

    def test_steps_are_byte_identical_per_trajectory(self) -> None:
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
        ]
        first = synthesize_code_block(trajectory)
        second = synthesize_code_block(trajectory)
        assert first is not None and second is not None
        assert first.steps == second.steps

    def test_truncated_trajectory_caps_steps_with_code(self) -> None:
        trajectory = [
            _interaction("click", selector=f'role=button[name="b{i}"]', source_url="https://example.com/")
            for i in range(_MAX_STEPS + 5)
        ]
        block = synthesize_code_block(trajectory)
        assert block is not None
        click_steps = [s for s in block.steps if s["action_type"] == "click"]
        assert len(click_steps) == _MAX_STEPS
        assert block.steps[-1]["line_end"] == len(block.code.splitlines())

    def test_steps_validate_against_code_block_step_schema(self) -> None:
        block = synthesize_code_block(_SCOUT_TRAJECTORY)
        assert block is not None
        validated = [CodeBlockStep(**step) for step in block.steps]
        assert all(step.line_start is not None and step.line_end is not None for step in validated)


class TestLineBoundaryEscaping:
    # str.splitlines() and several parsers treat each of these as a line boundary. An attacker-controlled
    # page can plant one in an accessible name or option value; left unescaped it splits the emitted
    # one-line literal across lines and corrupts the block (availability, not RCE — the leading quote
    # precedes the payload and every attacker quote is escaped).
    _BOUNDARY_CODEPOINTS = ("\x0b", "\x0c", "\x85", " ", " ")

    @staticmethod
    def _parses(code: str) -> ast.Module:
        wrapper = "async def __wrapper__(payload=None):\n" + "\n".join(f"    {line}" for line in code.splitlines())
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

    def test_broad_body_text_wait_for_function_surfaces_selection_diagnostic(self) -> None:
        code = (
            "await page.wait_for_function("
            "\"() => document.body.innerText.includes('Details') || "
            "document.body.innerText.includes('Nothing was found')\", timeout=5000)\n"
        )

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert sum(1 for d in diagnostics if d.code == "BROAD_DOCUMENT_BODY_TEXT_WAIT") == 1
        assert any("localized result/detail" in d.message for d in diagnostics)

    def test_broad_body_text_wait_for_function_keyword_expression_surfaces_selection_diagnostic(self) -> None:
        code = (
            "await page.wait_for_function("
            "expression=\"() => document.body.innerText.includes('Details')\", timeout=5000)\n"
        )

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert sum(1 for d in diagnostics if d.code == "BROAD_DOCUMENT_BODY_TEXT_WAIT") == 1

    def test_localized_detail_locator_wait_does_not_surface_body_text_diagnostic(self) -> None:
        code = (
            'await page.locator("main").get_by_text("Details").wait_for(timeout=5000)\n'
            'return {"entity_name": await page.locator("h1").inner_text(timeout=5000)}\n'
        )

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert not any(d.code == "BROAD_DOCUMENT_BODY_TEXT_WAIT" for d in diagnostics)

    def test_non_page_wait_for_function_does_not_surface_body_text_diagnostic(self) -> None:
        code = """
        diagnostics = []
        await custom_waiter.wait_for_function("() => document.body.innerText.includes('Ready')")
        return {"diagnostics": diagnostics}
        """

        diagnostics = preflight_code_block(code, parameter_keys=("custom_waiter",))

        assert not any(d.code == "BROAD_DOCUMENT_BODY_TEXT_WAIT" for d in diagnostics)

    def test_persist_safety_rejects_broad_body_text_wait_for_function(self) -> None:
        workflow_yaml = (
            "title: Record lookup\n"
            "workflow_definition:\n"
            "  blocks:\n"
            "    - block_type: code\n"
            "      label: check_record_status_status\n"
            "      code: |\n"
            "        await page.wait_for_function(\"() => document.body.innerText.includes('Details')\", "
            "timeout=5000)\n"
        )

        errors = _code_block_safety_errors(workflow_yaml, None)

        assert any("failed the generated-code preflight check" in str(error) for error in errors)
        assert any("localized result/detail" in str(error) for error in errors)

    def test_broad_container_record_scan_surfaces_row_extraction_diagnostic(self) -> None:
        code = """
        raw_cards = []
        for selector in ["[class*='result']", "article", "section", ".card", "li"]:
            locs = page.locator(selector)
            for i in range(await locs.count()):
                txt = await locs.nth(i).inner_text()
                if "status" in txt.lower():
                    raw_cards.append(txt)
        items = []
        for txt in raw_cards:
            items.append({
                "item_name": txt.split("\\n")[0],
                "address": txt[:200],
                "status": "Inactive" if "inactive" in txt.lower() else "Active",
            })
        return {"items": items, "overall_status": "Active"}
        """

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert any(d.code == "BROAD_TABLE_RECORD_SCAN" for d in diagnostics)

    def test_embedded_tr_substring_does_not_suppress_broad_scan_diagnostic(self) -> None:
        code = """
        cards = page.locator("section")
        items = []
        for i in range(await cards.count()):
            text = await cards.nth(i).inner_text()
            items.append({
                "record_name": text.split("\\n")[0],
                "street_label": "Street",
                "status": "Active",
            })
        return {"items": items}
        """

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert any(d.code == "BROAD_TABLE_RECORD_SCAN" for d in diagnostics)

    def test_non_selector_section_literal_does_not_surface_broad_scan_diagnostic(self) -> None:
        code = """
        layout_type = "section"
        items = [{"name": "Example"}]
        return {"items": items, "layout_type": layout_type}
        """

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert not any(d.code == "BROAD_TABLE_RECORD_SCAN" for d in diagnostics)

    def test_lone_list_item_selector_does_not_surface_broad_scan_diagnostic(self) -> None:
        code = """
        await page.locator("li").filter(has_text="Status").click()
        return {"items": [], "overall_status": "Active"}
        """

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert not any(d.code == "BROAD_TABLE_RECORD_SCAN" for d in diagnostics)

    def test_status_text_without_record_return_shape_does_not_surface_broad_scan_diagnostic(self) -> None:
        code = """
        sections = page.locator("section")
        for i in range(await sections.count()):
            text = await sections.nth(i).inner_text()
            if "status" in text.lower():
                print("status panel found")
        return {"ok": True}
        """

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert not any(d.code == "BROAD_TABLE_RECORD_SCAN" for d in diagnostics)

    def test_status_only_error_shape_does_not_surface_broad_scan_diagnostic(self) -> None:
        code = """
        section = page.locator("section").first
        if await section.count() == 0:
            return {"status": "missing"}
        await section.click()
        return {"status": "clicked"}
        """

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert not any(d.code == "BROAD_TABLE_RECORD_SCAN" for d in diagnostics)

    def test_table_row_record_extraction_does_not_surface_broad_scan_diagnostic(self) -> None:
        code = """
        rows = page.locator("table tbody tr")
        items = []
        for i in range(await rows.count()):
            row = rows.nth(i)
            cells = row.locator("td")
            if await cells.count() < 3:
                continue
            item_name = " ".join((await cells.nth(0).inner_text()).split())
            address = " ".join((await cells.nth(1).inner_text()).split())
            status = " ".join((await cells.nth(2).inner_text()).split())
            items.append({
                "item_name": item_name,
                "address": address,
                "status": status,
            })
        return {"items": items, "overall_status": "Active"}
        """

        diagnostics = preflight_code_block(code, parameter_keys=())

        assert not any(d.code == "BROAD_TABLE_RECORD_SCAN" for d in diagnostics)


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


class TestOfferTextGoalAndSteps:
    def test_offer_text_carries_steps_json_and_goal(self) -> None:
        synthesized = synthesize_code_block(_SCOUT_TRAJECTORY)
        assert synthesized is not None
        text = render_synthesized_offer_text(
            synthesized, _SCOUT_TRAJECTORY, goal="Search the catalog and add the item to the cart"
        )
        assert "`steps`" in text
        assert "`prompt`" in text
        assert "Search the catalog and add the item to the cart" in text
        assert '"action_type": "goto_url"' in text
        assert '"action_type": "input_text"' in text

    def test_offer_text_omits_goal_mention_without_goal(self) -> None:
        synthesized = synthesize_code_block(_SCOUT_TRAJECTORY)
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized, _SCOUT_TRAJECTORY)
        assert "`steps`" in text
        assert "`prompt`" not in text

    def test_offer_text_goal_quotes_and_newlines_stay_in_quoted_span(self) -> None:
        synthesized = synthesize_code_block(_SCOUT_TRAJECTORY)
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized, _SCOUT_TRAJECTORY, goal='find the "best" deal\nand report it')
        assert "`prompt` field to \"find the 'best' deal and report it\"" in text

    def test_offer_text_goal_code_fences_are_neutralized(self) -> None:
        synthesized = synthesize_code_block(_SCOUT_TRAJECTORY)
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized, _SCOUT_TRAJECTORY, goal="do this\n```python\nx\n```")
        assert "\n```python\nx\n```" not in text

    def test_offer_text_steps_json_matches_synthesized_steps(self) -> None:
        synthesized = synthesize_code_block(_SCOUT_TRAJECTORY)
        assert synthesized is not None
        text = render_synthesized_offer_text(synthesized, _SCOUT_TRAJECTORY)
        rendered = json.dumps(synthesized.steps, indent=2, sort_keys=True)
        assert rendered in text


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
        assert "observed_not_verified" not in text


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

    def test_totp_field_reads_runtime_otp_method(self) -> None:
        result = synthesize_code_block(
            [self._credential_fill(selector="#totpCode", credential_field="totp", typed_length=6)]
        )
        assert result is not None
        assert 'await page.locator("#totpCode").fill(await authtest_simple.otp())' in result.code

    def test_runtime_otp_fill_is_detected_as_credential_fill_code(self) -> None:
        assert code_contains_credential_fill('await page.locator("#otp").fill(await login_credential.otp())')

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
        assert ".username` / `.password` attributes and `.otp()`" in text
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

    def test_synthesized_credential_code_passes_persist_safety_seam(self) -> None:
        result = synthesize_code_block(
            [
                self._credential_fill(),
                self._credential_fill(selector="#passwordInput", credential_field="password"),
            ]
        )
        assert result is not None
        workflow_yaml = (
            "title: Login with saved credential\n"
            "workflow_definition:\n"
            "  parameters:\n"
            "    - parameter_type: workflow\n"
            "      workflow_parameter_type: credential_id\n"
            "      key: authtest_simple\n"
            "      default_value: cred_123\n"
            "  blocks:\n"
            "    - block_type: code\n"
            "      label: login_with_saved_credential\n"
            "      parameter_keys:\n"
            "        - authtest_simple\n"
            "      code: |\n" + "\n".join(f"        {line}" for line in result.code.splitlines()) + "\n"
        )

        assert _code_block_safety_errors(workflow_yaml, None) == []


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


_DOWNLOAD_SELECTOR = '[href="/files/report.pdf"]'


def _nav_click() -> dict[str, Any]:
    # The scout reaches the download page via a navigation click; the download affordance itself
    # is observed in nav_targets, so its selector is NOT this trajectory click.
    return _interaction("click", selector="div.stmt-row", source_url="https://example.com/bills")


def _download_target(**fields: Any) -> ReachedDownloadTarget:
    base: dict[str, Any] = {
        "selector": _DOWNLOAD_SELECTOR,
        "affordance_text": "Download PDF",
        "download_kind": "extension",
        "source_step": "trajectory_recency",
        "already_registered": False,
    }
    base.update(fields)
    return ReachedDownloadTarget(**base)


class TestDownloadRungSynthesis:
    def test_post_auth_resume_skips_login_prefix_without_download_target(self) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    CREDENTIAL_FILL_TOOL_NAME,
                    selector="#user",
                    source_url="https://example.com/bills",
                    credential_id="cred_123",
                    credential_name="mock_portal_login",
                    credential_field="username",
                ),
                _interaction("click", selector="#contBtn"),
                _interaction(
                    CREDENTIAL_FILL_TOOL_NAME,
                    selector="#pass",
                    credential_id="cred_123",
                    credential_name="mock_portal_login",
                    credential_field="password",
                ),
                _interaction("click", selector="#signinBtn"),
                _interaction("click", selector="#current-statement-row"),
            ],
        )
        assert result is not None
        lines = result.code.splitlines()
        assert lines[0] == "    _scout_entry_resume_after_auth = False"
        assert lines[1] == '    _scout_entry_target = page.locator("#user")'
        assert '        await page.goto("https://example.com/bills", wait_until="domcontentloaded")' in lines
        assert '            _scout_entry_resume_target = page.locator("#current-statement-row")' in lines
        assert "                _scout_entry_resume_after_auth = True" in lines
        assert "    if not _scout_entry_resume_after_auth:" in lines
        assert '        await page.locator("#user").fill(mock_portal_login.username)' in lines
        assert '    await page.locator("#current-statement-row").click()' in lines
        assert "_scout_entry_reused_current_page" not in result.code
        assert result.parameters == [{"key": "mock_portal_login", "credential_id": "cred_123"}]
        ast.parse("async def _block(page):\n" + result.code)

    def test_appended_terminal_step_compiled_from_typed_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = synthesize_code_block([_nav_click()], reached_download_target=_download_target())
        assert result is not None
        assert "_scout_entry_reused_current_page = False" in result.code
        assert 'await page.goto("https://example.com/bills", wait_until="domcontentloaded")' in result.code
        assert f"async with page.expect_download() as {_DOWNLOAD_VAR_BASE}:" in result.code
        download_obj = f"{_DOWNLOAD_VAR_BASE}_file"
        assert f"{download_obj} = await {_DOWNLOAD_VAR_BASE}.value" in result.code
        assert f"await {download_obj}.path()" in result.code
        assert '"downloaded_file_name": downloaded_file_name' in result.code
        assert '"download_url"' not in result.code
        assert '"downloaded_file_path"' not in result.code
        assert '"downloaded_files"' not in result.code
        # The execution-layer dir-diff registers the single landed file, so the synthesizer never save_as.
        assert "save_as" not in result.code
        # The click inside expect_download targets the TYPED download selector, not the navigation click.
        download_step = result.code.split("async with page.expect_download")[1]
        assert 'await page.locator("[href=\\"/files/report.pdf\\"]").click()' in download_step
        assert "div.stmt-row" not in download_step
        # A download does not navigate, so no trailing load-wait inside the appended step.
        assert 'wait_for_load_state("load")' not in download_step

    def test_already_registered_emits_no_download_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = synthesize_code_block(
            [_nav_click()], reached_download_target=_download_target(already_registered=True, selector="")
        )
        assert result is not None
        assert "expect_download" not in result.code

    def test_target_none_byte_identical_to_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        trajectory = [_nav_click()]
        base = synthesize_code_block(trajectory)
        none_target = synthesize_code_block(trajectory, reached_download_target=None)
        assert base is not None and none_target is not None
        assert base.code == none_target.code
        assert "expect_download" not in none_target.code

    def test_non_download_trajectory_emits_no_download_terminal(self) -> None:
        trajectory = [
            _interaction("type_text", selector="#user", source_url="https://example.com/", typed_value="abc"),
            _interaction("select_option", selector="#state", value="CA"),
            _interaction(
                "fill_credential_field",
                selector="#pw",
                credential_id="cred_123",
                credential_field="password",
                credential_name="Login",
            ),
            _interaction("press_key", selector="#user", key="Enter"),
            _interaction("click", selector="#submit", source_url="https://example.com/"),
        ]
        result = synthesize_code_block(trajectory)
        assert result is not None
        assert "expect_download" not in result.code

    def test_user_param_named_dl_info_is_renamed_via_reserved_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = synthesize_code_block(
            [
                _interaction(
                    "type_text",
                    selector="#field",
                    source_url="https://example.com/",
                    typed_value="x",
                    accessible_name=_DOWNLOAD_VAR_BASE,
                ),
                _nav_click(),
            ],
            reached_download_target=_download_target(),
        )
        assert result is not None
        param_keys = [p["key"] for p in result.parameters]
        assert _DOWNLOAD_VAR_BASE not in param_keys
        assert f"{_DOWNLOAD_VAR_BASE}_field" in param_keys
        assert f"async with page.expect_download() as {_DOWNLOAD_VAR_BASE}:" in result.code

    def test_emitted_download_snippet_is_safe_and_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = synthesize_code_block([_nav_click()], reached_download_target=_download_target())
        assert result is not None
        wrapped = "async def _block(page):\n" + result.code
        CodeBlock.is_safe_code(wrapped)
        assert not any(d.code == "SYNTAX_ERROR" for d in preflight_code_block(result.code, parameter_keys=()))
        ast.parse(wrapped)

    def test_download_snippet_awaits_completion_without_save_as(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = synthesize_code_block([_nav_click()], reached_download_target=_download_target())
        assert result is not None
        download_obj = f"{_DOWNLOAD_VAR_BASE}_file"
        assert result.code.count(f"{download_obj} = await {_DOWNLOAD_VAR_BASE}.value") == 1
        # Awaiting the path() completes the download into the run-scoped dir; the SKY-10937 dir-diff
        # registers the single file when available; the returned summary keeps the filename JSON-safe.
        assert f"await {download_obj}.path()" in result.code
        assert "return {" in result.code
        assert '"downloaded_file_name": downloaded_file_name' in result.code
        assert '"downloaded_file_path"' not in result.code
        assert '"download_url"' not in result.code
        assert '"downloaded_files"' not in result.code
        assert "save_as" not in result.code
        CodeBlock.is_safe_code("async def _block(page):\n" + result.code)

    def test_download_offer_text_only_present_for_download_snippet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        download = synthesize_code_block([_nav_click()], reached_download_target=_download_target())
        plain = synthesize_code_block([_interaction("click", selector="#go", source_url="https://example.com/")])
        assert download is not None and plain is not None
        assert "expect_download" in render_synthesized_offer_text(download)
        assert "expect_download" not in render_synthesized_offer_text(plain)

    def test_non_live_call_sites_compile_without_kwarg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = synthesize_code_block([_nav_click()])
        assert result is not None
        assert "expect_download" not in result.code
