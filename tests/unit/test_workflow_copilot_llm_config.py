"""Tests for the workflow-copilot v2 LLM key wiring (SKY-10642).

Optional settings give operators independent control over the main Copilot lane,
agent-specific lane, and fast-consumer lane:
``WORKFLOW_COPILOT_LLM_KEY``, ``WORKFLOW_COPILOT_AGENT_LLM_KEY``, and
``WORKFLOW_COPILOT_FAST_LLM_KEY``. ``WORKFLOW_COPILOT_LITE_LLM_KEY`` is the
dedicated raw-secret safety lane and never falls back to the acting model.
These tests cover the public contract: defaults, fallback chains, and
PostHog → env-specific → default resolution order.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.config import Settings
from skyvern.forge.sdk.copilot import llm_config as copilot_llm_config
from skyvern.forge.sdk.copilot import tools as copilot_tools
from skyvern.forge.sdk.routes import workflow_copilot as workflow_copilot_route

# ---------------------------------------------------------------------------
# Settings field defaults
# ---------------------------------------------------------------------------


def test_workflow_copilot_agent_llm_key_default_is_none() -> None:
    assert Settings.model_fields["WORKFLOW_COPILOT_LLM_KEY"].default is None
    assert Settings.model_fields["WORKFLOW_COPILOT_AGENT_LLM_KEY"].default is None


def test_workflow_copilot_fast_llm_key_default_is_none() -> None:
    assert Settings.model_fields["WORKFLOW_COPILOT_FAST_LLM_KEY"].default is None


def test_workflow_copilot_lite_llm_key_default_is_none() -> None:
    assert Settings.model_fields["WORKFLOW_COPILOT_LITE_LLM_KEY"].default is None


@pytest.mark.asyncio
async def test_resolve_raw_secret_safety_handler_uses_dedicated_posthog_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dedicated = object()

    async def _lookup(prompt_type: str, *_args: object) -> object:
        assert prompt_type == "workflow-copilot-raw-secret-safety"
        return dedicated

    monkeypatch.setattr(copilot_llm_config, "get_llm_handler_for_prompt_type", _lookup)
    assert await copilot_llm_config.resolve_raw_secret_safety_handler("wpid_1", "org_1") is dedicated


@pytest.mark.asyncio
async def test_resolve_raw_secret_safety_handler_has_no_main_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _lookup(*_args: object) -> None:
        return None

    monkeypatch.setattr(copilot_llm_config, "get_llm_handler_for_prompt_type", _lookup)
    monkeypatch.setattr(copilot_llm_config, "app", SimpleNamespace(WORKFLOW_COPILOT_LITE_LLM_API_HANDLER=None))
    assert await copilot_llm_config.resolve_raw_secret_safety_handler("wpid_1", "org_1") is None


class _AppHolderStub:
    """Mimic the AppHolder proxy: missing attributes raise RuntimeError, not
    AttributeError. The main handler fallback must catch both."""

    def __init__(self, **attrs: Any) -> None:
        for key, value in attrs.items():
            setattr(self, key, value)

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"ForgeApp is not initialized (accessed {name})")


# ---------------------------------------------------------------------------
# main Copilot handler fallback chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_resolve_copilot_agent_handler_delegates_to_main_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_handler = object()

    async def _main_lookup(workflow_permanent_id: str | None, organization_id: str | None) -> object:
        assert workflow_permanent_id == "wpid_1"
        assert organization_id == "org_1"
        return main_handler

    monkeypatch.setattr(workflow_copilot_route, "resolve_main_copilot_handler", _main_lookup)

    handler = await workflow_copilot_route._resolve_copilot_agent_handler("wpid_1", "org_1")
    assert handler is main_handler


@pytest.mark.asyncio
async def test_resolve_main_copilot_handler_posthog_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    posthog_handler = object()
    dedicated = object()
    primary = object()

    async def _posthog_lookup(prompt_type: str, *_args: object, **_kwargs: object) -> object:
        assert prompt_type == "workflow-copilot"
        return posthog_handler

    monkeypatch.setattr(copilot_llm_config, "get_llm_handler_for_prompt_type", _posthog_lookup)
    monkeypatch.setattr(
        copilot_llm_config,
        "app",
        SimpleNamespace(
            WORKFLOW_COPILOT_AGENT_LLM_API_HANDLER=dedicated,
            WORKFLOW_COPILOT_LLM_API_HANDLER=primary,
            LLM_API_HANDLER=primary,
        ),
    )

    handler = await copilot_llm_config.resolve_main_copilot_handler("wpid_1", "org_1")
    assert handler is posthog_handler


@pytest.mark.asyncio
async def test_resolve_main_copilot_handler_falls_back_to_dedicated(monkeypatch: pytest.MonkeyPatch) -> None:
    dedicated = object()
    primary = object()

    async def _posthog_lookup(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(copilot_llm_config, "get_llm_handler_for_prompt_type", _posthog_lookup)
    monkeypatch.setattr(
        copilot_llm_config,
        "app",
        SimpleNamespace(
            WORKFLOW_COPILOT_AGENT_LLM_API_HANDLER=dedicated,
            LLM_API_HANDLER=primary,
        ),
    )

    handler = await copilot_llm_config.resolve_main_copilot_handler("wpid_1", "org_1")
    assert handler is dedicated


@pytest.mark.asyncio
async def test_resolve_main_copilot_handler_falls_back_to_workflow_copilot_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_copilot = object()
    primary = object()

    async def _posthog_lookup(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(copilot_llm_config, "get_llm_handler_for_prompt_type", _posthog_lookup)
    monkeypatch.setattr(
        copilot_llm_config,
        "app",
        SimpleNamespace(
            WORKFLOW_COPILOT_AGENT_LLM_API_HANDLER=None,
            WORKFLOW_COPILOT_LLM_API_HANDLER=workflow_copilot,
            LLM_API_HANDLER=primary,
        ),
    )

    handler = await copilot_llm_config.resolve_main_copilot_handler("wpid_1", "org_1")
    assert handler is workflow_copilot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_app",
    [
        # A plain object lacking the dedicated attribute raises AttributeError.
        pytest.param(lambda primary: SimpleNamespace(LLM_API_HANDLER=primary), id="attribute_error"),
        # AppHolder.__getattr__ raises bare RuntimeError pre-startup, not AttributeError.
        pytest.param(lambda primary: _AppHolderStub(LLM_API_HANDLER=primary), id="runtime_error"),
        # A custom forge-app initializer that sets the new attribute to None must fall through.
        pytest.param(
            lambda primary: SimpleNamespace(
                WORKFLOW_COPILOT_AGENT_LLM_API_HANDLER=None,
                WORKFLOW_COPILOT_LLM_API_HANDLER=primary,
                LLM_API_HANDLER=object(),
            ),
            id="dedicated_is_none",
        ),
    ],
)
async def test_resolve_main_copilot_handler_falls_back_to_primary(
    monkeypatch: pytest.MonkeyPatch, make_app: Any
) -> None:
    primary = object()

    async def _posthog_lookup(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(copilot_llm_config, "get_llm_handler_for_prompt_type", _posthog_lookup)
    monkeypatch.setattr(copilot_llm_config, "app", make_app(primary))

    handler = await copilot_llm_config.resolve_main_copilot_handler("wpid_1", "org_1")
    assert handler is primary


# ---------------------------------------------------------------------------
# non-narration Copilot helpers use the main lane
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_verification_handler_uses_main_copilot_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_handler = object()

    async def _main_lookup(workflow_permanent_id: str | None, organization_id: str | None) -> object:
        assert workflow_permanent_id == "wpid_1"
        assert organization_id == "org_1"
        return main_handler

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.completion.resolve_main_copilot_handler",
        _main_lookup,
    )
    ctx: Any = SimpleNamespace(workflow_permanent_id="wpid_1", organization_id="org_1")

    handler = await copilot_tools._completion_verification_handler(ctx)
    assert handler is main_handler


@pytest.mark.asyncio
async def test_composition_visual_handler_uses_fast_copilot_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_handler = object()

    async def _fast_lookup(workflow_permanent_id: str | None, organization_id: str | None) -> object:
        assert workflow_permanent_id == "wpid_1"
        assert organization_id == "org_1"
        return fast_handler

    monkeypatch.setattr(copilot_tools.composition_capture, "resolve_fast_copilot_handler", _fast_lookup)
    ctx: Any = SimpleNamespace(workflow_permanent_id="wpid_1", organization_id="org_1")

    handler = await copilot_tools._composition_visual_handler(ctx)
    assert handler is fast_handler
