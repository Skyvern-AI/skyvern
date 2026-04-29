"""Tests for the copilot tracing_setup module's initialization sequence.

Covers the disabled path, the logfire-available path, and the logfire-missing
path. Stubs logfire's and agents' public surface so no real logfire internals
are mutated. The private-API patches in `_patch_agent_span_attributes` are
covered by an integration test once the copilot runtime wiring lands.
"""

from __future__ import annotations

import contextlib
import sys
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.copilot import tracing_setup


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing_setup, "_TRACING_INITIALIZED", False)
    monkeypatch.setattr(tracing_setup, "_SPAN_RENAME_WARNED", threading.Event())
    monkeypatch.delenv("COPILOT_TRACING_ENABLED", raising=False)


@pytest.fixture
def agents_stub(monkeypatch: pytest.MonkeyPatch) -> tuple[list[bool], list[Any]]:
    disabled_calls: list[bool] = []
    processors_calls: list[Any] = []
    monkeypatch.setitem(
        sys.modules,
        "agents",
        SimpleNamespace(
            set_tracing_disabled=lambda flag: disabled_calls.append(flag),
            set_trace_processors=lambda procs: processors_calls.append(procs),
        ),
    )
    return disabled_calls, processors_calls


@pytest.fixture
def stub_logger(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    logged: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        tracing_setup,
        "LOG",
        SimpleNamespace(
            warning=lambda msg, **kwargs: logged.append((msg, kwargs)),
            info=lambda msg, **kwargs: None,
        ),
    )
    return logged


def test_disabled_path(agents_stub: tuple[list[bool], list[Any]]) -> None:
    disabled_calls, processors_calls = agents_stub

    span = tracing_setup.copilot_span("name")

    assert isinstance(span, contextlib.nullcontext)
    assert tracing_setup._TRACING_INITIALIZED is False

    tracing_setup.ensure_tracing_initialized()
    assert disabled_calls == [True]
    assert processors_calls == []
    assert tracing_setup._TRACING_INITIALIZED is True


def test_enabled_with_logfire(
    monkeypatch: pytest.MonkeyPatch,
    agents_stub: tuple[list[bool], list[Any]],
) -> None:
    monkeypatch.setenv("COPILOT_TRACING_ENABLED", "1")
    _, processors_calls = agents_stub

    configure_calls: list[dict[str, Any]] = []
    instrument_calls: list[None] = []
    patch_calls: list[None] = []

    monkeypatch.setitem(
        sys.modules,
        "logfire",
        SimpleNamespace(
            configure=lambda **kw: configure_calls.append(kw),
            instrument_openai_agents=lambda: instrument_calls.append(None),
        ),
    )
    monkeypatch.setattr(tracing_setup, "_patch_agent_span_attributes", lambda: patch_calls.append(None))

    tracing_setup.ensure_tracing_initialized()

    assert len(configure_calls) == 1
    assert configure_calls[0]["send_to_logfire"] == "if-token-present"
    assert configure_calls[0]["service_name"] == "skyvern-copilot"
    assert configure_calls[0]["environment"] == settings.ENV
    assert instrument_calls == [None]
    assert patch_calls == [None]
    assert processors_calls == [[]]


def test_enabled_without_logfire(
    monkeypatch: pytest.MonkeyPatch,
    agents_stub: tuple[list[bool], list[Any]],
    stub_logger: list[tuple[str, dict[str, Any]]],
) -> None:
    monkeypatch.setenv("COPILOT_TRACING_ENABLED", "1")
    _, processors_calls = agents_stub

    # Setting sys.modules[name] = None makes subsequent `import name` raise ImportError.
    monkeypatch.setitem(sys.modules, "logfire", None)
    patch_calls: list[None] = []
    monkeypatch.setattr(tracing_setup, "_patch_agent_span_attributes", lambda: patch_calls.append(None))

    tracing_setup.ensure_tracing_initialized()

    assert any("logfire is not installed" in msg for msg, _ in stub_logger)
    assert processors_calls == [[]]
    assert patch_calls == []


def test_warn_span_rename_once(stub_logger: list[tuple[str, dict[str, Any]]]) -> None:
    tracing_setup._warn_span_rename_once(RuntimeError("first"))
    tracing_setup._warn_span_rename_once(RuntimeError("second"))

    assert len(stub_logger) == 1
    assert "first" in stub_logger[0][1]["error"]


class TestTracingSetup:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes "])
    def test_is_tracing_enabled_truthy_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv("COPILOT_TRACING_ENABLED", value)

        from skyvern.forge.sdk.copilot.tracing_setup import is_tracing_enabled

        assert is_tracing_enabled() is True

    def test_is_tracing_enabled_defaults_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("COPILOT_TRACING_ENABLED", raising=False)

        from skyvern.forge.sdk.copilot.tracing_setup import is_tracing_enabled

        assert is_tracing_enabled() is False

    def test_ensure_tracing_initialized_sets_processors_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import skyvern.forge.sdk.copilot.tracing_setup as tracing_setup

        monkeypatch.setenv("COPILOT_TRACING_ENABLED", "1")
        monkeypatch.setattr(tracing_setup, "_TRACING_INITIALIZED", False)

        configure_calls: list[dict[str, Any]] = []
        instrument_calls: list[None] = []
        # logfire is an optional dependency and may not be installed — inject a
        # stub module into sys.modules so `import logfire` inside
        # `ensure_tracing_initialized` resolves to our recorder.
        monkeypatch.setitem(
            sys.modules,
            "logfire",
            SimpleNamespace(
                configure=lambda **kw: configure_calls.append(kw),
                instrument_openai_agents=lambda: instrument_calls.append(None),
            ),
        )
        monkeypatch.setattr(tracing_setup, "_patch_agent_span_attributes", lambda: None)

        tracing_setup.ensure_tracing_initialized()
        tracing_setup.ensure_tracing_initialized()

        assert configure_calls == [
            {
                "send_to_logfire": "if-token-present",
                "service_name": "skyvern-copilot",
                "environment": settings.ENV,
            }
        ]
        assert instrument_calls == [None]

    def test_copilot_span_returns_nullcontext_when_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import skyvern.forge.sdk.copilot.tracing_setup as tracing_setup

        monkeypatch.delenv("COPILOT_TRACING_ENABLED", raising=False)
        monkeypatch.setattr(
            tracing_setup,
            "ensure_tracing_initialized",
            lambda: pytest.fail("ensure_tracing_initialized should not be called"),
        )

        span = tracing_setup.copilot_span("test_span", {"value": 1})

        with span:
            pass

    def test_copilot_span_uses_custom_span_when_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import skyvern.forge.sdk.copilot.tracing_setup as tracing_setup

        monkeypatch.setenv("COPILOT_TRACING_ENABLED", "1")
        monkeypatch.setattr(tracing_setup, "ensure_tracing_initialized", lambda: None)

        captured: dict[str, Any] = {}

        class FakeSpan:
            def __enter__(self) -> FakeSpan:
                return self

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
                return False

        fake_span = FakeSpan()

        def fake_custom_span(name: str, data: dict[str, Any] | None = None) -> FakeSpan:
            captured["name"] = name
            captured["data"] = data
            return fake_span

        monkeypatch.setattr("agents.tracing.custom_span", fake_custom_span)

        span = tracing_setup.copilot_span("run_blocks", {"block_count": 2})

        assert span is fake_span
        assert captured == {"name": "run_blocks", "data": {"block_count": 2}}


class TestPatchAgentSpanAttributes:
    """Tests that invoke `_patch_agent_span_attributes`. The autouse fixture
    snapshots and restores both mutated logfire callables so tests never leak
    patch state to the rest of the suite — `_patch_agent_span_attributes` is
    not idempotent.

    These tests exercise the real logfire integration and are skipped when
    logfire is not installed (it's an optional runtime dependency).
    """

    @pytest.fixture(autouse=True)
    def _restore_oai_mod(self) -> Any:
        pytest.importorskip("logfire")
        import logfire._internal.integrations.openai_agents as _oai_mod

        orig_attrs = _oai_mod.attributes_from_span_data
        orig_create = _oai_mod.LogfireTraceProviderWrapper.create_span
        try:
            yield
        finally:
            _oai_mod.attributes_from_span_data = orig_attrs
            _oai_mod.LogfireTraceProviderWrapper.create_span = orig_create

    def test_patch_adds_otel_agent_attributes(self) -> None:
        from agents import AgentSpanData

        from skyvern.forge.sdk.copilot.tracing_setup import (
            _copilot_model_name,
            _patch_agent_span_attributes,
        )

        _patch_agent_span_attributes()

        import logfire._internal.integrations.openai_agents as _oai_mod

        token = _copilot_model_name.set("gpt-4o")
        try:
            span_data = AgentSpanData(name="workflow-copilot", handoffs=[], tools=[], output_type="str")
            attrs = _oai_mod.attributes_from_span_data(span_data, "Agent run: {name!r}")
        finally:
            _copilot_model_name.reset(token)

        assert attrs["gen_ai.agent.name"] == "workflow-copilot"
        assert attrs["gen_ai.operation.name"] == "invoke_agent"
        assert attrs["gen_ai.provider.name"] == "openai"
        assert attrs["gen_ai.request.model"] == "gpt-4o"
        assert attrs["name"] == "workflow-copilot"

    def test_patch_sets_otel_span_name_on_create_span(self) -> None:
        import logfire._internal.integrations.openai_agents as _oai_mod
        from agents import AgentSpanData

        from skyvern.forge.sdk.copilot.tracing_setup import _patch_agent_span_attributes

        mock_logfire_span = MagicMock()
        mock_logfire_span._span_name = "Agent run: {name!r}"
        mock_result = MagicMock()
        mock_result.span_helper.span = mock_logfire_span

        def fake_create(
            self: Any, span_data: Any, span_id: Any = None, parent: Any = None, disabled: bool = False
        ) -> Any:
            return mock_result

        _oai_mod.LogfireTraceProviderWrapper.create_span = fake_create

        _patch_agent_span_attributes()

        span_data = AgentSpanData(
            name="workflow-copilot",
            handoffs=[],
            tools=[],
            output_type="str",
        )
        _oai_mod.LogfireTraceProviderWrapper.create_span(MagicMock(), span_data)

        assert mock_logfire_span._span_name == "invoke_agent workflow-copilot"

    def test_patch_redacts_function_span_input_and_sets_tool_semconv(self) -> None:
        import json

        import logfire._internal.integrations.openai_agents as _oai_mod
        from agents import FunctionSpanData

        from skyvern.forge.sdk.copilot.tracing_setup import _patch_agent_span_attributes

        _patch_agent_span_attributes()

        raw_input = json.dumps({"username": "alice", "password": "hunter2"})
        span_data = FunctionSpanData(name="update_workflow", input=raw_input, output=None)
        attrs = _oai_mod.attributes_from_span_data(span_data, "Function: {name!r}")

        assert attrs["gen_ai.operation.name"] == "execute_tool"
        assert attrs["gen_ai.tool.name"] == "update_workflow"
        # Tool-call secrets must not leak to the trace backend.
        assert "hunter2" not in str(attrs.get("input", ""))
