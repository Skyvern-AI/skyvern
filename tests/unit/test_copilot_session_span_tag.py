"""Tests for the copilot.session_id span attribute on LLM spans."""

from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from skyvern.forge.sdk.api.llm.api_handler_factory import _enrich_llm_span
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext


def _call_enrich(span: MagicMock) -> None:
    _enrich_llm_span(
        span,
        model="gpt-5",
        prompt_name="workflow-copilot",
        prompt_tokens=10,
        completion_tokens=20,
        reasoning_tokens=0,
        cached_tokens=0,
        latency_ms=100,
        llm_cost=0.001,
    )


def _set_attribute_keys(span: MagicMock) -> list[str]:
    return [call.args[0] for call in span.set_attribute.call_args_list if call.args]


class TestEnrichLlmSpan:
    def test_stamps_attribute_when_context_has_session_id(self) -> None:
        span = MagicMock()
        with skyvern_context.scoped(SkyvernContext(copilot_session_id="chat_xyz")):
            _call_enrich(span)
        span.set_attribute.assert_any_call("copilot.session_id", "chat_xyz")

    def test_no_attribute_when_context_has_no_session_id(self) -> None:
        span = MagicMock()
        with skyvern_context.scoped(SkyvernContext(copilot_session_id=None)):
            _call_enrich(span)
        assert "copilot.session_id" not in _set_attribute_keys(span)

    def test_no_attribute_when_no_ambient_context(self) -> None:
        span = MagicMock()
        skyvern_context.reset()
        _call_enrich(span)
        assert "copilot.session_id" not in _set_attribute_keys(span)


class _FakeAgentSpanData:
    def __init__(self, name: str = "workflow-copilot") -> None:
        self.name = name


class _FakeGenerationSpanData:
    pass


class _FakeFunctionSpanData:
    def __init__(self, name: str = "some_tool") -> None:
        self.name = name


def _install_patch(monkeypatch: Any) -> Any:
    # Wire ModuleType stubs for the full logfire chain — sys.modules entries alone aren't enough.
    import sys

    from skyvern.forge.sdk.copilot import tracing_setup

    def _fake_original(span_data: Any, msg_template: str) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        if isinstance(span_data, _FakeAgentSpanData):
            attrs["name"] = span_data.name
        if isinstance(span_data, _FakeFunctionSpanData):
            attrs["name"] = span_data.name
        return attrs

    class _FakeWrapper:
        @staticmethod
        def create_span(*args: Any, **kwargs: Any) -> Any:
            return None

    logfire_mod = ModuleType("logfire")
    internal_mod = ModuleType("logfire._internal")
    integrations_mod = ModuleType("logfire._internal.integrations")
    oai_mod = ModuleType("logfire._internal.integrations.openai_agents")
    oai_mod.attributes_from_span_data = _fake_original  # type: ignore[attr-defined]
    oai_mod.LogfireTraceProviderWrapper = _FakeWrapper  # type: ignore[attr-defined]
    logfire_mod._internal = internal_mod  # type: ignore[attr-defined]
    internal_mod.integrations = integrations_mod  # type: ignore[attr-defined]
    integrations_mod.openai_agents = oai_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "logfire", logfire_mod)
    monkeypatch.setitem(sys.modules, "logfire._internal", internal_mod)
    monkeypatch.setitem(sys.modules, "logfire._internal.integrations", integrations_mod)
    monkeypatch.setitem(sys.modules, "logfire._internal.integrations.openai_agents", oai_mod)
    monkeypatch.setitem(
        sys.modules,
        "agents",
        SimpleNamespace(
            AgentSpanData=_FakeAgentSpanData,
            GenerationSpanData=_FakeGenerationSpanData,
            FunctionSpanData=_FakeFunctionSpanData,
        ),
    )

    tracing_setup._patch_agent_span_attributes()
    return oai_mod.attributes_from_span_data


class TestPatchedSpanAttributes:
    def test_stamps_on_agent_span_when_context_has_session_id(self, monkeypatch: Any) -> None:
        patched = _install_patch(monkeypatch)
        with skyvern_context.scoped(SkyvernContext(copilot_session_id="chat_xyz")):
            attrs = patched(_FakeAgentSpanData(), "Agent run: {name!r}")
        assert attrs["copilot.session_id"] == "chat_xyz"

    def test_stamps_on_generation_span_when_context_has_session_id(self, monkeypatch: Any) -> None:
        patched = _install_patch(monkeypatch)
        with skyvern_context.scoped(SkyvernContext(copilot_session_id="chat_xyz")):
            attrs = patched(_FakeGenerationSpanData(), "Generation")
        assert attrs["copilot.session_id"] == "chat_xyz"

    def test_stamps_on_function_span_when_context_has_session_id(self, monkeypatch: Any) -> None:
        patched = _install_patch(monkeypatch)
        with skyvern_context.scoped(SkyvernContext(copilot_session_id="chat_xyz")):
            attrs = patched(_FakeFunctionSpanData(), "Function call")
        assert attrs["copilot.session_id"] == "chat_xyz"

    def test_no_attribute_when_context_has_no_session_id(self, monkeypatch: Any) -> None:
        patched = _install_patch(monkeypatch)
        with skyvern_context.scoped(SkyvernContext(copilot_session_id=None)):
            attrs_agent = patched(_FakeAgentSpanData(), "Agent run: {name!r}")
            attrs_gen = patched(_FakeGenerationSpanData(), "Generation")
            attrs_fn = patched(_FakeFunctionSpanData(), "Function call")
        assert "copilot.session_id" not in attrs_agent
        assert "copilot.session_id" not in attrs_gen
        assert "copilot.session_id" not in attrs_fn

    def test_no_attribute_when_no_ambient_context(self, monkeypatch: Any) -> None:
        patched = _install_patch(monkeypatch)
        skyvern_context.reset()
        attrs = patched(_FakeAgentSpanData(), "Agent run: {name!r}")
        assert "copilot.session_id" not in attrs
