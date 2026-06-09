"""Tests for SKY-10075 / SKY-9717: SLIM_LLM_OUTPUT_PROMPTS tiered variants.

Covers the strict template contract (slim_output is None | 'safe' | 'terse'),
golden byte-identity of the control render, per-variant static-prefix contract,
the run-level variant resolver, the canonical prompt-family maps, the effective
per-call telemetry label, and the cache-variant key.

When intentionally changing the control render of an in-scope template, refresh
the snapshots with `uv run python tests/unit/golden_prompts/regenerate.py` and
review the golden diff like production code.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.experimentation import slim_llm_output
from skyvern.forge.sdk.experimentation.prompt_families import (
    PROMPT_NAMES_BY_FAMILY,
    SLIM_ENABLED_FAMILIES,
    SLIM_VARIANT_SAFE,
    SLIM_VARIANT_TERSE,
    TEMPLATES_BY_FAMILY,
    PromptFamily,
    effective_prompt_schema_variant,
    family_for_prompt_name,
    family_for_template,
)
from skyvern.forge.sdk.experimentation.slim_llm_output import get_slim_output_template_value
from skyvern.forge.sdk.prompting import PromptEngine

GOLDEN_DIR = Path(__file__).parent / "golden_prompts"
TEMPLATE_DIR = Path(__file__).parent.parent.parent / "skyvern" / "forge" / "prompts" / "skyvern"

TERSE_MARKER = "Maximum 15 words"


@pytest.fixture
def prompt_engine() -> PromptEngine:
    return PromptEngine(model="skyvern")


@pytest.fixture
def run_context() -> Any:
    context = SkyvernContext(workflow_run_id="wr_123", task_id="tsk_123", organization_id="org_456")
    skyvern_context.set(context)
    yield context
    skyvern_context.reset()


def _mock_provider(monkeypatch: pytest.MonkeyPatch, variant: Any) -> MagicMock:
    provider = MagicMock()
    if isinstance(variant, Exception):
        provider.get_value_cached = AsyncMock(side_effect=variant)
    else:
        provider.get_value_cached = AsyncMock(return_value=variant)
    mock_app = MagicMock()
    mock_app.EXPERIMENTATION_PROVIDER = provider
    monkeypatch.setattr(slim_llm_output, "app", mock_app)
    return provider


_EXTRACT_ACTION_KWARGS: dict[str, Any] = {
    "navigation_goal": "test goal",
    "navigation_payload_str": "{}",
    "starting_url": "https://example.com",
    "current_url": "https://example.com",
    "data_extraction_goal": None,
    "action_history": "[]",
    "error_code_mapping_str": None,
    "local_datetime": "2025-01-01T00:00:00",
    "verification_code_check": True,
    "complete_criterion": None,
    "terminate_criterion": None,
    "show_close_page_action": False,
    "open_tabs_context": None,
    "recent_dialog_messages_str": None,
    "llm_screenshots_enabled": True,
    "enriched_tree_enabled": False,
    "elements": "<html></html>",
}
_CHECK_USER_GOAL_KWARGS: dict[str, Any] = {
    "navigation_goal": "test goal",
    "navigation_payload": "{}",
    "complete_criterion": None,
    "action_history": "[]",
    "new_elements_ids": None,
    "without_screenshots": False,
    "local_datetime": "2025-01-01T00:00:00",
    "elements": "<html></html>",
}

# template -> (render kwargs, fields dropped in slim, fields kept in every variant, has terse marker)
_TEMPLATE_CASES: dict[str, tuple[dict[str, Any], list[str], list[str], bool]] = {
    "extract-action": (
        _EXTRACT_ACTION_KWARGS,
        [
            '"user_goal_stage":',
            '"user_goal_achieved": bool, // True if the user goal has been completed, otherwise False.',
            '"action_plan":',
            '"thought":',
        ],
        [
            '"reasoning":',
            '"user_detail_query":',
            '"user_detail_answer":',
            '"confidence_float":',
            '"verification_code_reasoning":',
            '"place_to_enter_verification_code":',
            '"should_verify_by_magic_link":',
        ],
        True,
    ),
    "extract-action-static": (
        _EXTRACT_ACTION_KWARGS,
        [
            '"user_goal_stage":',
            '"user_goal_achieved": bool, // True if the user goal has been completed, otherwise False.',
            '"action_plan":',
            '"thought":',
        ],
        ['"reasoning":', '"verification_code_reasoning":'],
        True,
    ),
    "check-user-goal": (
        _CHECK_USER_GOAL_KWARGS,
        ['"page_info":'],
        ['"thoughts":', '"user_goal_achieved":'],
        True,
    ),
    "check-user-goal-with-termination": (
        {**_CHECK_USER_GOAL_KWARGS, "terminate_criterion": None},
        ['"page_info":'],
        ['"thoughts":', '"status":', '"failure_categories":'],
        True,
    ),
    "auto-completion-choose-option": (
        {
            "is_search": False,
            "field_information": "name",
            "filled_value": "John",
            "navigation_goal": "test goal",
            "navigation_payload_str": "{}",
            "elements": "<html></html>",
            "new_elements_ids": None,
            "local_datetime": "2025-01-01T00:00:00",
        },
        ['"thought":', '"reasoning":'],
        ['"auto_completion_attempt":', '"confidence_float":', '"value":', '"id":'],
        False,
    ),
    "parse-input-or-select-context": (
        {"element_id": "elem_1", "action_reasoning": "test reasoning", "elements": "<html></html>"},
        ['"thought":'],
        ['"field":', '"is_required":', '"is_text_captcha":'],
        False,
    ),
}


# ---------------------------------------------------------------------------
# Template contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", _TEMPLATE_CASES)
def test_control_render_is_byte_identical_to_golden(prompt_engine: PromptEngine, template_name: str) -> None:
    # The golden files were generated from the pre-slim production templates; any
    # control-render drift means the flag-off path changed behavior.
    kwargs, _, _, _ = _TEMPLATE_CASES[template_name]
    golden = (GOLDEN_DIR / f"{template_name}.control.txt").read_text()
    assert prompt_engine.load_prompt(template_name, **kwargs) == golden
    assert prompt_engine.load_prompt(template_name, slim_output=None, **kwargs) == golden


@pytest.mark.parametrize("template_name", _TEMPLATE_CASES)
@pytest.mark.parametrize("variant", ["safe", "terse"])
def test_slim_render_drops_dead_fields_and_keeps_consumed_fields(
    prompt_engine: PromptEngine, template_name: str, variant: str
) -> None:
    kwargs, drops, keeps, _ = _TEMPLATE_CASES[template_name]
    control = prompt_engine.load_prompt(template_name, **kwargs)
    slim = prompt_engine.load_prompt(template_name, slim_output=variant, **kwargs)
    for field in drops:
        assert field in control, f"{field} missing from {template_name} control render"
        assert field not in slim, f"{field} should be dropped from {template_name} when slim_output={variant}"
    for field in keeps:
        assert field in slim, f"{field} must be kept in {template_name} when slim_output={variant}"
    assert len(slim) < len(control)


@pytest.mark.parametrize("template_name", _TEMPLATE_CASES)
def test_terse_marker_only_in_terse_renders(prompt_engine: PromptEngine, template_name: str) -> None:
    kwargs, _, _, has_terse = _TEMPLATE_CASES[template_name]
    assert TERSE_MARKER not in prompt_engine.load_prompt(template_name, **kwargs)
    assert TERSE_MARKER not in prompt_engine.load_prompt(template_name, slim_output="safe", **kwargs)
    assert (TERSE_MARKER in prompt_engine.load_prompt(template_name, slim_output="terse", **kwargs)) == has_terse


@pytest.mark.parametrize("variant", [None, "safe", "terse"])
def test_extract_action_static_is_verbatim_prefix_in_every_variant(
    prompt_engine: PromptEngine, variant: str | None
) -> None:
    # The cached prompt path renders static + dynamic separately and joins them; if the
    # static file stops being a verbatim prefix, the cached path silently diverges.
    kwargs = dict(_EXTRACT_ACTION_KWARGS)
    if variant is not None:
        kwargs["slim_output"] = variant
    full = prompt_engine.load_prompt("extract-action", **kwargs)
    static = prompt_engine.load_prompt("extract-action-static", **kwargs)
    assert full.startswith(static.rstrip()), f"static is not a prefix of complete when slim_output={variant}"


def test_templates_never_use_bare_slim_output_truthiness() -> None:
    # "off" (or any string) is truthy in Jinja: a bare {% if slim_output %} would slim
    # the control cohort the moment a caller passes a string. Only explicit
    # membership/equality checks are allowed.
    bare_truthiness = re.compile(r"\{%-?\s*if\s+(not\s+)?slim_output\s*[-]?%\}")
    for template_path in TEMPLATE_DIR.glob("*.j2"):
        content = template_path.read_text()
        assert not bare_truthiness.search(content), f"bare slim_output truthiness check in {template_path.name}"
        for usage in re.findall(r"\{%-?\s*if[^%]*slim_output[^%]*%\}", content):
            assert ("not in" in usage) or ("==" in usage), (
                f"non-explicit slim_output check in {template_path.name}: {usage}"
            )


# ---------------------------------------------------------------------------
# Canonical family maps
# ---------------------------------------------------------------------------


def test_family_maps_are_consistent_and_disjoint() -> None:
    assert set(TEMPLATES_BY_FAMILY) == set(PromptFamily)
    assert set(PROMPT_NAMES_BY_FAMILY) == set(PromptFamily)
    all_templates = [t for templates in TEMPLATES_BY_FAMILY.values() for t in templates]
    all_prompt_names = [p for prompt_names in PROMPT_NAMES_BY_FAMILY.values() for p in prompt_names]
    assert len(all_templates) == len(set(all_templates)), "a template maps to two families"
    assert len(all_prompt_names) == len(set(all_prompt_names)), "a prompt_name maps to two families"
    assert SLIM_ENABLED_FAMILIES <= set(PromptFamily)


def test_family_lookups() -> None:
    assert family_for_template("extract-action") == PromptFamily.EXTRACT_ACTIONS
    assert family_for_template("extract-action-static") == PromptFamily.EXTRACT_ACTIONS
    assert family_for_template("check-user-goal-with-termination") == PromptFamily.CHECK_USER_GOAL
    assert family_for_prompt_name("extract-actions") == PromptFamily.EXTRACT_ACTIONS
    assert family_for_prompt_name("check-user-goal-after-click") == PromptFamily.CHECK_USER_GOAL
    assert family_for_template("decisive-criterion-validate") is None
    assert family_for_prompt_name("extract-information") is None
    assert family_for_template(None) is None
    assert family_for_prompt_name(None) is None


def test_effective_prompt_schema_variant() -> None:
    assert effective_prompt_schema_variant(SLIM_VARIANT_SAFE, "extract-actions") == SLIM_VARIANT_SAFE
    assert effective_prompt_schema_variant(SLIM_VARIANT_TERSE, "extract-actions") == SLIM_VARIANT_TERSE
    # check-user-goal is intentionally excluded from the allowlist — its calls must
    # label control even under a treatment run.
    assert effective_prompt_schema_variant(SLIM_VARIANT_SAFE, "check-user-goal") is None
    assert effective_prompt_schema_variant(SLIM_VARIANT_TERSE, "check-user-goal-after-click") is None
    # Other families outside the allowlist render control, so their calls must be labeled control.
    assert effective_prompt_schema_variant(SLIM_VARIANT_SAFE, "auto-completion-choose-option") is None
    assert effective_prompt_schema_variant(SLIM_VARIANT_SAFE, "parse-input-or-select-context") is None
    # Unknown prompt_names and non-slim assignments fail safe to control.
    assert effective_prompt_schema_variant(SLIM_VARIANT_SAFE, "extract-information") is None
    assert effective_prompt_schema_variant(None, "extract-actions") is None
    assert effective_prompt_schema_variant("control", "extract-actions") is None
    assert effective_prompt_schema_variant("garbage", "extract-actions") is None


# ---------------------------------------------------------------------------
# Run-level variant resolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flag_variant,expected_template_value",
    [(SLIM_VARIANT_SAFE, "safe"), (SLIM_VARIANT_TERSE, "terse"), ("control", None), (None, None), ("garbage", None)],
)
async def test_resolver_maps_flag_variant_to_template_value(
    monkeypatch: pytest.MonkeyPatch, run_context: SkyvernContext, flag_variant: Any, expected_template_value: Any
) -> None:
    provider = _mock_provider(monkeypatch, flag_variant)
    assert await get_slim_output_template_value("extract-action") == expected_template_value
    provider.get_value_cached.assert_awaited_once_with(
        "SLIM_LLM_OUTPUT_PROMPTS",
        "wr_123",
        properties={"organization_id": "org_456", "workflow_permanent_id": None},
    )


@pytest.mark.asyncio
async def test_resolver_resolves_once_per_run(monkeypatch: pytest.MonkeyPatch, run_context: SkyvernContext) -> None:
    provider = _mock_provider(monkeypatch, SLIM_VARIANT_SAFE)
    assert await get_slim_output_template_value("extract-action") == "safe"
    assert await get_slim_output_template_value("extract-action-static") == "safe"
    assert await get_slim_output_template_value("extract-action-dynamic") == "safe"
    assert provider.get_value_cached.await_count == 1
    assert run_context.slim_output_variant_assigned == SLIM_VARIANT_SAFE
    assert run_context.slim_output_variant_resolved is True


@pytest.mark.asyncio
async def test_resolver_excludes_families_outside_allowlist(
    monkeypatch: pytest.MonkeyPatch, run_context: SkyvernContext
) -> None:
    _mock_provider(monkeypatch, SLIM_VARIANT_SAFE)
    assert await get_slim_output_template_value("auto-completion-choose-option") is None
    assert await get_slim_output_template_value("parse-input-or-select-context") is None
    # check-user-goal is intentionally excluded — its templates must render control
    # even under a treatment run.
    assert await get_slim_output_template_value("check-user-goal") is None
    assert await get_slim_output_template_value("check-user-goal-with-termination") is None
    # The assignment is still recorded for telemetry even though these render control.
    assert run_context.slim_output_variant_assigned == SLIM_VARIANT_SAFE


@pytest.mark.asyncio
async def test_resolver_returns_none_for_unknown_template(
    monkeypatch: pytest.MonkeyPatch, run_context: SkyvernContext
) -> None:
    _mock_provider(monkeypatch, SLIM_VARIANT_SAFE)
    assert await get_slim_output_template_value("decisive-criterion-validate") is None
    assert await get_slim_output_template_value("single-click-action") is None


@pytest.mark.asyncio
async def test_resolver_pins_run_to_control_on_flag_error(
    monkeypatch: pytest.MonkeyPatch, run_context: SkyvernContext
) -> None:
    provider = _mock_provider(monkeypatch, RuntimeError("posthog down"))
    assert await get_slim_output_template_value("extract-action") is None
    assert run_context.slim_output_variant_assigned is None
    assert run_context.slim_output_variant_resolved is True
    # Resolution is pinned for the whole run — no per-call re-evaluation flapping.
    assert await get_slim_output_template_value("extract-action") is None
    assert provider.get_value_cached.await_count == 1


@pytest.mark.asyncio
async def test_resolver_returns_none_without_context(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_provider(monkeypatch, SLIM_VARIANT_SAFE)
    skyvern_context.reset()
    assert await get_slim_output_template_value("extract-action") is None


@pytest.mark.asyncio
async def test_concurrent_first_use_resolves_exactly_once(
    monkeypatch: pytest.MonkeyPatch, run_context: SkyvernContext
) -> None:
    # Parallel prompt builds (speculative extract-actions + verification) can hit the
    # resolver before any resolution exists; single-flight must guarantee one flag
    # evaluation and one consistent cohort for the whole run.
    import asyncio

    provider = MagicMock()

    async def _slow_get_value(*args: Any, **kwargs: Any) -> str:
        await asyncio.sleep(0.02)
        return SLIM_VARIANT_SAFE

    provider.get_value_cached = AsyncMock(side_effect=_slow_get_value)
    mock_app = MagicMock()
    mock_app.EXPERIMENTATION_PROVIDER = provider
    monkeypatch.setattr(slim_llm_output, "app", mock_app)

    results = await asyncio.gather(
        get_slim_output_template_value("extract-action"),
        get_slim_output_template_value("extract-action-static"),
        get_slim_output_template_value("extract-action"),
    )
    assert results == ["safe", "safe", "safe"]
    assert provider.get_value_cached.await_count == 1
    assert run_context.slim_output_variant_assigned == SLIM_VARIANT_SAFE


@pytest.mark.asyncio
async def test_concurrent_first_use_with_flag_error_pins_all_callers_to_control(
    monkeypatch: pytest.MonkeyPatch, run_context: SkyvernContext
) -> None:
    import asyncio

    provider = MagicMock()

    async def _slow_failure(*args: Any, **kwargs: Any) -> str:
        await asyncio.sleep(0.02)
        raise RuntimeError("posthog down")

    provider.get_value_cached = AsyncMock(side_effect=_slow_failure)
    mock_app = MagicMock()
    mock_app.EXPERIMENTATION_PROVIDER = provider
    monkeypatch.setattr(slim_llm_output, "app", mock_app)

    results = await asyncio.gather(
        get_slim_output_template_value("extract-action"),
        get_slim_output_template_value("extract-action-static"),
    )
    assert results == [None, None]
    assert provider.get_value_cached.await_count == 1
    assert run_context.slim_output_variant_assigned is None
    assert run_context.slim_output_variant_resolved is True


@pytest.mark.asyncio
async def test_resolver_pins_control_without_run_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _mock_provider(monkeypatch, SLIM_VARIANT_SAFE)
    context = SkyvernContext(organization_id="org_456")
    skyvern_context.set(context)
    try:
        assert await get_slim_output_template_value("extract-action") is None
        assert await get_slim_output_template_value("extract-action") is None
        provider.get_value_cached.assert_not_awaited()
        # Pinned resolved so later renders skip the lock instead of re-entering forever.
        assert context.slim_output_variant_resolved is True
        assert context.slim_output_variant_assigned is None
    finally:
        skyvern_context.reset()


# ---------------------------------------------------------------------------
# Cache-variant key
# ---------------------------------------------------------------------------


def test_cache_variant_encodes_slim_arm_and_control_key_is_unchanged() -> None:
    from skyvern.forge.agent import ForgeAgent

    build = ForgeAgent._build_extract_action_cache_variant
    control = build(verification_code_check=False, show_close_page_action=False, complete_criterion=None)
    safe = build(
        verification_code_check=False, show_close_page_action=False, complete_criterion=None, slim_output="safe"
    )
    terse = build(
        verification_code_check=False, show_close_page_action=False, complete_criterion=None, slim_output="terse"
    )
    assert control == "std"
    assert safe == "slim_safe"
    assert terse == "slim_terse"
    assert len({control, safe, terse}) == 3

    otp_control = build(verification_code_check=True, show_close_page_action=False, complete_criterion=None)
    otp_safe = build(
        verification_code_check=True, show_close_page_action=False, complete_criterion=None, slim_output="safe"
    )
    assert otp_control == "vc"
    assert otp_safe == "vc-slim_safe"
