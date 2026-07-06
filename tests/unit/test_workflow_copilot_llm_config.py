"""Tests for the workflow-copilot v2 LLM key wiring (SKY-10642).

Two optional settings give operators independent control over (a) the main
Copilot reasoning/guardrail/evidence lane and (b) the fast-consumer lane:
``WORKFLOW_COPILOT_AGENT_LLM_KEY`` and ``WORKFLOW_COPILOT_FAST_LLM_KEY``.
These tests cover the public contract: defaults, fallback chains, and
PostHog → env-specific → default resolution order.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.config import Settings
from skyvern.forge.sdk.copilot import agent as copilot_agent
from skyvern.forge.sdk.copilot import llm_config as copilot_llm_config
from skyvern.forge.sdk.copilot import narration
from skyvern.forge.sdk.copilot import tools as copilot_tools
from skyvern.forge.sdk.routes import workflow_copilot as workflow_copilot_route

# ---------------------------------------------------------------------------
# Settings field defaults
# ---------------------------------------------------------------------------


def test_workflow_copilot_agent_llm_key_default_is_none() -> None:
    assert Settings.model_fields["WORKFLOW_COPILOT_AGENT_LLM_KEY"].default is None


def test_workflow_copilot_narration_llm_key_default_is_none() -> None:
    assert Settings.model_fields["WORKFLOW_COPILOT_FAST_LLM_KEY"].default is None


# ---------------------------------------------------------------------------
# _get_narrator_handler fallback chain
# ---------------------------------------------------------------------------


class _AppHolderStub:
    """Mimic the AppHolder proxy: missing attributes raise RuntimeError, not
    AttributeError. The narration fallback must catch both."""

    def __init__(self, **attrs: Any) -> None:
        for key, value in attrs.items():
            setattr(self, key, value)

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"ForgeApp is not initialized (accessed {name})")


def test_narrator_handler_prefers_dedicated_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    dedicated = object()
    secondary = object()
    monkeypatch.setattr(
        copilot_llm_config,
        "app",
        SimpleNamespace(
            WORKFLOW_COPILOT_FAST_LLM_API_HANDLER=dedicated,
            SECONDARY_LLM_API_HANDLER=secondary,
        ),
    )
    assert narration._get_narrator_handler() is dedicated


@pytest.mark.parametrize(
    "make_app",
    [
        # A plain object lacking the dedicated attribute raises AttributeError.
        pytest.param(lambda secondary: SimpleNamespace(SECONDARY_LLM_API_HANDLER=secondary), id="attribute_error"),
        # AppHolder.__getattr__ raises bare RuntimeError pre-startup, not AttributeError.
        pytest.param(lambda secondary: _AppHolderStub(SECONDARY_LLM_API_HANDLER=secondary), id="runtime_error"),
        # A custom forge-app initializer that sets the new attribute to None must not disable narration.
        pytest.param(
            lambda secondary: SimpleNamespace(
                WORKFLOW_COPILOT_FAST_LLM_API_HANDLER=None,
                SECONDARY_LLM_API_HANDLER=secondary,
            ),
            id="dedicated_is_none",
        ),
    ],
)
def test_narrator_handler_falls_back_to_secondary(monkeypatch: pytest.MonkeyPatch, make_app: Any) -> None:
    secondary = object()
    monkeypatch.setattr(copilot_llm_config, "app", make_app(secondary))
    assert narration._get_narrator_handler() is secondary


def test_narrator_handler_returns_none_when_both_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(copilot_llm_config, "app", _AppHolderStub())
    assert narration._get_narrator_handler() is None


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
                LLM_API_HANDLER=primary,
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
# resolve_narrator_handler PostHog override + env-driven fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_narrator_handler_posthog_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    posthog_handler = object()
    fast = object()

    async def _posthog_lookup(prompt_type: str, *_args: object, **_kwargs: object) -> object:
        assert prompt_type == "workflow-copilot-fast"
        return posthog_handler

    monkeypatch.setattr(copilot_llm_config, "get_llm_handler_for_prompt_type", _posthog_lookup)
    monkeypatch.setattr(
        copilot_llm_config,
        "app",
        SimpleNamespace(WORKFLOW_COPILOT_FAST_LLM_API_HANDLER=fast, SECONDARY_LLM_API_HANDLER=object()),
    )

    handler = await narration.resolve_narrator_handler("wpid_1", "org_1")
    assert handler is posthog_handler


@pytest.mark.asyncio
async def test_resolve_narrator_handler_falls_back_to_fast_when_posthog_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast = object()

    async def _posthog_lookup(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(copilot_llm_config, "get_llm_handler_for_prompt_type", _posthog_lookup)
    monkeypatch.setattr(
        copilot_llm_config,
        "app",
        SimpleNamespace(WORKFLOW_COPILOT_FAST_LLM_API_HANDLER=fast, SECONDARY_LLM_API_HANDLER=object()),
    )

    handler = await narration.resolve_narrator_handler("wpid_1", "org_1")
    assert handler is fast


@pytest.mark.asyncio
async def test_resolve_narrator_handler_falls_back_when_posthog_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """PostHog can raise (network, AppHolder pre-startup, payload parse error).
    Narration must never propagate; fall through to the env-driven handler."""
    fast = object()

    async def _raising_lookup(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("posthog down")

    monkeypatch.setattr(copilot_llm_config, "get_llm_handler_for_prompt_type", _raising_lookup)
    monkeypatch.setattr(
        copilot_llm_config,
        "app",
        SimpleNamespace(WORKFLOW_COPILOT_FAST_LLM_API_HANDLER=fast, SECONDARY_LLM_API_HANDLER=object()),
    )

    handler = await narration.resolve_narrator_handler("wpid_1", "org_1")
    assert handler is fast


@pytest.mark.asyncio
async def test_resolve_narrator_handler_skips_posthog_when_ids_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """PostHog needs both distinct_id and organization_id to evaluate the
    flag; without them, skip the lookup and go straight to env-driven."""
    fast = object()
    posthog_called = False

    async def _posthog_lookup(*_args: object, **_kwargs: object) -> object:
        nonlocal posthog_called
        posthog_called = True
        return object()

    monkeypatch.setattr(copilot_llm_config, "get_llm_handler_for_prompt_type", _posthog_lookup)
    monkeypatch.setattr(
        copilot_llm_config,
        "app",
        SimpleNamespace(WORKFLOW_COPILOT_FAST_LLM_API_HANDLER=fast, SECONDARY_LLM_API_HANDLER=object()),
    )

    handler = await narration.resolve_narrator_handler(None, "org_1")
    assert handler is fast
    assert posthog_called is False

    handler = await narration.resolve_narrator_handler("wpid_1", None)
    assert handler is fast
    assert posthog_called is False


# ---------------------------------------------------------------------------
# non-narration Copilot helpers use the main lane
# ---------------------------------------------------------------------------


def test_resolve_request_policy_handler_uses_main_copilot_handler() -> None:
    main_handler = object()
    assert copilot_agent._resolve_request_policy_handler(main_handler) is main_handler


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
