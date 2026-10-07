from __future__ import annotations

import copy
import pickle
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from agents import ModelSettings, function_tool

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.cache_envelope import (
    CacheableSystemInstructions,
    ExplicitCacheEnvelope,
    build_explicit_cache_envelope,
    item_parts_key,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy

pytestmark = pytest.mark.usefixtures("gpt56_litellm_models")

_CODE_ONLY_HEADER = "ACTIVE BLOCK AUTHORING POLICY: CODE-ONLY BROWSER MODE"


@function_tool
def _first_tool(value: str) -> str:
    return value


@function_tool
def _second_tool(value: str) -> str:
    return value.upper()


def _cache_body(
    *,
    namespace: str = "wcc_one",
    stable_prefix: str = "stable",
    model: str = "gpt-5.6-sol",
    tools: list[Any] | None = None,
) -> ExplicitCacheEnvelope:
    result = build_explicit_cache_envelope(
        model=model,
        base_url=None,
        system_instructions=CacheableSystemInstructions(
            stable_prefix,
            "dynamic",
            cache_namespace=namespace,
        ),
        input=[{"role": "user", "content": "hello"}],
        model_settings=ModelSettings(),
        tools=tools or [_first_tool],
        handoffs=[],
        reasoning_effort="medium",
    )
    assert result is not None
    return result


def test_cache_key_is_stable_and_binds_session_model_prefix_and_tool_surface() -> None:
    original = _cache_body()
    assert _cache_body().prompt_cache_key == original.prompt_cache_key
    assert _cache_body(namespace="wcc_two").prompt_cache_key != original.prompt_cache_key
    assert _cache_body(stable_prefix="other").prompt_cache_key != original.prompt_cache_key
    assert _cache_body(model="gpt-5.6-terra").prompt_cache_key != original.prompt_cache_key
    assert _cache_body(tools=[_second_tool]).prompt_cache_key != original.prompt_cache_key


def test_responses_envelope_marks_only_the_stable_system_prefix() -> None:
    result = _cache_body(stable_prefix="stable instructions")

    assert result.logical_messages[0] == {
        "role": "system",
        "content": "stable instructionsdynamic",
    }
    assert result.responses_input[0] == {
        "type": "message",
        "role": "system",
        "content": [
            {
                "type": "input_text",
                "text": "stable instructions",
                "prompt_cache_breakpoint": {"mode": "explicit"},
            },
            {
                "type": "input_text",
                "text": "dynamic",
            },
        ],
    }
    assert result.extra_body == {"prompt_cache_options": {"mode": "explicit"}}
    assert result.prompt_cache_key.startswith("copilot:")


_ASSISTANT_TEXT_THEN_CALL: list[Any] = [
    {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "x", "annotations": []}]},
    {"type": "function_call", "call_id": "call_1", "name": "_first_tool", "arguments": "{}"},
    {"type": "function_call_output", "call_id": "call_1", "output": [{"type": "input_text", "text": "done"}]},
]


@pytest.mark.parametrize(("anchor", "keyed_roles"), [(0, ["system", "user"]), (1, ["system"]), (3, ["system", None])])
def test_rolling_breakpoint_lands_only_on_an_anchor_whose_items_prefix_the_request(
    anchor: int, keyed_roles: list[str | None]
) -> None:
    result = build_explicit_cache_envelope(
        model="gpt-5.6-sol",
        base_url=None,
        system_instructions=CacheableSystemInstructions("stable", "dynamic", cache_namespace="wcc_one"),
        input=_ASSISTANT_TEXT_THEN_CALL,
        model_settings=ModelSettings(),
        tools=[_first_tool],
        handoffs=[],
        reasoning_effort="medium",
        anchors=[anchor],
    )

    assert result is not None
    keyed = [
        item.get("role")
        for item in result.responses_input
        for part in item.get(item_parts_key(item)) or []
        if isinstance(part, dict) and "prompt_cache_breakpoint" in part
    ]
    assert keyed == keyed_roles
    assert result.breakpoint_count == len(keyed_roles)


def test_system_prompt_text_is_identical_to_direct_template_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 7, 30, 12, 34, 56, tzinfo=timezone.utc)

    class _FixedDateTime:
        @classmethod
        def now(cls, tz: timezone) -> datetime:
            return fixed_now

    monkeypatch.setattr(agent_module, "datetime", _FixedDateTime)
    config = agent_module.CopilotConfig()
    expected = agent_module.prompt_engine.load_prompt(
        template=config.prompt_template.removesuffix(".j2"),
        current_datetime=fixed_now.isoformat(),
        tool_usage_guide="tool guide",
        security_rules=config.security_rules,
    )

    actual = agent_module._build_system_prompt(tool_usage_guide="tool guide", config=config)

    assert isinstance(actual, CacheableSystemInstructions)
    # The envelope adds nothing to the rendered template except the code-owned MCP authority
    # clause and current-time fact, neither of which a template may drop or displace.
    assert actual.stable_prefix == f"{agent_module._MCP_RESULT_SECURITY_BOUNDARY}\n\n{expected}"
    assert str(actual) == (
        f"{agent_module._MCP_RESULT_SECURITY_BOUNDARY}\n\n{expected}"
        f"\n\n{agent_module._CURRENT_TIME_FACT_PREFIX}{fixed_now.isoformat()}"
    )


def test_default_template_carries_the_current_time_fact_in_the_dynamic_tail() -> None:
    prompt = agent_module._build_system_prompt(tool_usage_guide="tool guide")

    assert isinstance(prompt, CacheableSystemInstructions)
    assert agent_module._CURRENT_TIME_FACT_PREFIX not in prompt.stable_prefix
    assert str(prompt).count(agent_module._CURRENT_TIME_FACT_PREFIX) == 1
    _, _, carried = prompt.dynamic_suffix.partition(agent_module._CURRENT_TIME_FACT_PREFIX)
    carried_at = datetime.fromisoformat(carried.strip())
    assert carried_at.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - carried_at).total_seconds()) < 300


def test_template_without_a_datetime_placeholder_still_carries_the_time_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_load_prompt(**_kwargs: str) -> str:
        return "custom template body with no datetime placeholder"

    monkeypatch.setattr(agent_module.prompt_engine, "load_prompt", fake_load_prompt)

    prompt = agent_module._build_system_prompt(tool_usage_guide="tool guide")

    assert isinstance(prompt, CacheableSystemInstructions)
    assert prompt.stable_prefix.endswith("custom template body with no datetime placeholder")
    assert agent_module._CURRENT_TIME_FACT_PREFIX not in prompt.stable_prefix
    _, _, carried = prompt.dynamic_suffix.partition(agent_module._CURRENT_TIME_FACT_PREFIX)
    carried_at = datetime.fromisoformat(carried.strip())
    assert abs((datetime.now(timezone.utc) - carried_at).total_seconds()) < 300


def test_time_fact_survives_the_dynamic_prompt_early_returns() -> None:
    instructions = agent_module._build_dynamic_system_prompt(
        tool_usage_guide="tool guide",
        config=agent_module.CopilotConfig(),
    )

    for context in (object(), SimpleNamespace(context=object())):
        prompt = instructions(context, None)
        assert agent_module._CURRENT_TIME_FACT_PREFIX in str(prompt)


def test_stable_prefix_is_byte_identical_across_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    def _prompt_at(moment: datetime) -> CacheableSystemInstructions:
        class _FixedDateTime:
            @classmethod
            def now(cls, tz: timezone) -> datetime:
                return moment

        monkeypatch.setattr(agent_module, "datetime", _FixedDateTime)
        built = agent_module._build_system_prompt(tool_usage_guide="tool guide")
        assert isinstance(built, CacheableSystemInstructions)
        return built

    first = _prompt_at(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    second = _prompt_at(datetime(2027, 8, 9, 10, 11, 12, tzinfo=timezone.utc))

    assert first.stable_prefix == second.stable_prefix
    assert first.dynamic_suffix != second.dynamic_suffix
    assert "2027-08-09T10:11:12" in second.dynamic_suffix


def test_system_prompt_places_datetime_and_runtime_context_after_breakpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered_kwargs: dict[str, str] = {}

    def fake_load_prompt(*, current_datetime: str, **kwargs: str) -> str:
        rendered_kwargs.update(kwargs)
        return f"stable prefix\n{current_datetime}\nstatic template tail"

    monkeypatch.setattr(agent_module.prompt_engine, "load_prompt", fake_load_prompt)
    base_prompt = agent_module._build_system_prompt(tool_usage_guide="tools")
    assert isinstance(base_prompt, CacheableSystemInstructions)
    assert base_prompt.stable_prefix == f"{agent_module._MCP_RESULT_SECURITY_BOUNDARY}\n\nstable prefix\n"
    assert "SKYVERN_COPILOT_DYNAMIC_DATETIME_BOUNDARY" not in str(base_prompt)
    assert "static template tail" in base_prompt.dynamic_suffix
    assert rendered_kwargs["tool_usage_guide"] == "tools"

    monkeypatch.setattr(agent_module, "_build_system_prompt", lambda **_: base_prompt)
    instructions = agent_module._build_dynamic_system_prompt(
        tool_usage_guide="tools",
        config=agent_module.CopilotConfig(),
    )
    prompt = instructions(
        SimpleNamespace(
            context=CopilotContext(
                organization_id="org_one",
                workflow_id="workflow_one",
                workflow_permanent_id="wpid_one",
                workflow_yaml="",
                browser_session_id=None,
                stream=SimpleNamespace(),  # type: ignore[arg-type]
                workflow_copilot_chat_id="wcc_one",
                request_policy=RequestPolicy(),
            )
        ),
        None,
    )

    assert isinstance(prompt, CacheableSystemInstructions)
    assert prompt.cache_namespace == "wcc_one"
    assert prompt.stable_prefix == base_prompt.stable_prefix
    assert base_prompt.dynamic_suffix in prompt.dynamic_suffix
    assert "TURN SAFETY AND REQUEST CONTEXT" in prompt.dynamic_suffix
    assert str(prompt) == prompt.stable_prefix + prompt.dynamic_suffix


def test_no_authoring_policy_section_reaches_either_prompt_half() -> None:
    config = agent_module.CopilotConfig(block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER)

    base_prompt = agent_module._build_system_prompt(tool_usage_guide="tools", config=config)
    prompt = agent_module._build_dynamic_system_prompt(tool_usage_guide="tools", config=config)(
        SimpleNamespace(
            context=CopilotContext(
                organization_id="org_one",
                workflow_id="workflow_one",
                workflow_permanent_id="wpid_one",
                workflow_yaml="",
                browser_session_id=None,
                stream=SimpleNamespace(),  # type: ignore[arg-type]
                workflow_copilot_chat_id="wcc_one",
                request_policy=RequestPolicy(),
            )
        ),
        None,
    )

    assert _CODE_ONLY_HEADER not in str(base_prompt)
    assert isinstance(prompt, CacheableSystemInstructions)
    assert _CODE_ONLY_HEADER not in prompt.stable_prefix
    assert _CODE_ONLY_HEADER not in prompt.dynamic_suffix


@pytest.mark.parametrize("duplicate", [copy.copy, copy.deepcopy, lambda value: pickle.loads(pickle.dumps(value))])
def test_cacheable_system_instructions_survive_the_copy_litellm_makes(duplicate: Any) -> None:
    original = CacheableSystemInstructions("stable ", "dynamic", cache_namespace="ns")

    duplicated = duplicate(original)

    assert duplicated == original
    assert (duplicated.stable_prefix, duplicated.dynamic_suffix, duplicated.cache_namespace) == (
        "stable ",
        "dynamic",
        "ns",
    )
