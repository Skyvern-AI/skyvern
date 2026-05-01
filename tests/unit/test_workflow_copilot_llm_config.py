"""Tests for the workflow-copilot v2 LLM key wiring (SKY-9398).

Two optional settings give operators independent control over (a) the
Agents-SDK reasoning loop and (b) narration: ``WORKFLOW_COPILOT_AGENT_LLM_KEY``
and ``WORKFLOW_COPILOT_FAST_LLM_KEY``. These tests cover the public
contract: defaults, narration handler fallback chain, and the route helper's
PostHog → dedicated → primary resolution order.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.config import Settings
from skyvern.forge.sdk.copilot import narration
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
        narration,
        "app",
        SimpleNamespace(
            WORKFLOW_COPILOT_FAST_LLM_API_HANDLER=dedicated,
            SECONDARY_LLM_API_HANDLER=secondary,
        ),
    )
    assert narration._get_narrator_handler() is dedicated


def test_narrator_handler_falls_back_to_secondary_on_attribute_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain object lacking the dedicated attribute raises ``AttributeError``;
    the fallback must catch that branch as well as the holder's ``RuntimeError``."""
    secondary = object()
    monkeypatch.setattr(
        narration,
        "app",
        SimpleNamespace(SECONDARY_LLM_API_HANDLER=secondary),
    )
    assert narration._get_narrator_handler() is secondary


def test_narrator_handler_falls_back_to_secondary_on_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AppHolder.__getattr__`` raises bare ``RuntimeError`` pre-startup,
    not ``AttributeError`` — the fallback must catch both."""
    secondary = object()
    monkeypatch.setattr(
        narration,
        "app",
        _AppHolderStub(SECONDARY_LLM_API_HANDLER=secondary),
    )
    assert narration._get_narrator_handler() is secondary


def test_narrator_handler_falls_back_to_secondary_when_dedicated_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: a custom forge-app initializer that sets the new attribute
    to ``None`` must not silently disable narration when SECONDARY is wired."""
    secondary = object()
    monkeypatch.setattr(
        narration,
        "app",
        SimpleNamespace(
            WORKFLOW_COPILOT_FAST_LLM_API_HANDLER=None,
            SECONDARY_LLM_API_HANDLER=secondary,
        ),
    )
    assert narration._get_narrator_handler() is secondary


def test_narrator_handler_returns_none_when_both_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(narration, "app", _AppHolderStub())
    assert narration._get_narrator_handler() is None


# ---------------------------------------------------------------------------
# _resolve_copilot_agent_handler fallback chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_agent_handler_posthog_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    posthog_handler = object()
    dedicated = object()
    primary = object()

    async def _posthog_lookup(*_args: object, **_kwargs: object) -> object:
        return posthog_handler

    monkeypatch.setattr(workflow_copilot_route, "get_llm_handler_for_prompt_type", _posthog_lookup)
    monkeypatch.setattr(
        workflow_copilot_route,
        "app",
        SimpleNamespace(
            WORKFLOW_COPILOT_AGENT_LLM_API_HANDLER=dedicated,
            LLM_API_HANDLER=primary,
        ),
    )

    handler = await workflow_copilot_route._resolve_copilot_agent_handler("wpid_1", "org_1")
    assert handler is posthog_handler


@pytest.mark.asyncio
async def test_resolve_agent_handler_falls_back_to_dedicated(monkeypatch: pytest.MonkeyPatch) -> None:
    dedicated = object()
    primary = object()

    async def _posthog_lookup(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(workflow_copilot_route, "get_llm_handler_for_prompt_type", _posthog_lookup)
    monkeypatch.setattr(
        workflow_copilot_route,
        "app",
        SimpleNamespace(
            WORKFLOW_COPILOT_AGENT_LLM_API_HANDLER=dedicated,
            LLM_API_HANDLER=primary,
        ),
    )

    handler = await workflow_copilot_route._resolve_copilot_agent_handler("wpid_1", "org_1")
    assert handler is dedicated


@pytest.mark.asyncio
async def test_resolve_agent_handler_falls_back_to_primary_on_attribute_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain object lacking the dedicated attribute raises ``AttributeError``."""
    primary = object()

    async def _posthog_lookup(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(workflow_copilot_route, "get_llm_handler_for_prompt_type", _posthog_lookup)
    monkeypatch.setattr(
        workflow_copilot_route,
        "app",
        SimpleNamespace(LLM_API_HANDLER=primary),
    )

    handler = await workflow_copilot_route._resolve_copilot_agent_handler("wpid_1", "org_1")
    assert handler is primary


@pytest.mark.asyncio
async def test_resolve_agent_handler_falls_back_to_primary_on_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AppHolder.__getattr__`` raises bare ``RuntimeError`` pre-startup, not
    ``AttributeError`` — the helper must catch both."""
    primary = object()

    async def _posthog_lookup(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(workflow_copilot_route, "get_llm_handler_for_prompt_type", _posthog_lookup)
    monkeypatch.setattr(workflow_copilot_route, "app", _AppHolderStub(LLM_API_HANDLER=primary))

    handler = await workflow_copilot_route._resolve_copilot_agent_handler("wpid_1", "org_1")
    assert handler is primary


@pytest.mark.asyncio
async def test_resolve_agent_handler_falls_back_when_dedicated_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = object()

    async def _posthog_lookup(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(workflow_copilot_route, "get_llm_handler_for_prompt_type", _posthog_lookup)
    monkeypatch.setattr(
        workflow_copilot_route,
        "app",
        SimpleNamespace(
            WORKFLOW_COPILOT_AGENT_LLM_API_HANDLER=None,
            LLM_API_HANDLER=primary,
        ),
    )

    handler = await workflow_copilot_route._resolve_copilot_agent_handler("wpid_1", "org_1")
    assert handler is primary


# ---------------------------------------------------------------------------
# resolve_narrator_handler PostHog override + env-driven fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_narrator_handler_posthog_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    posthog_handler = object()
    fast = object()

    async def _posthog_lookup(prompt_type: str, *_args: object, **_kwargs: object) -> object:
        assert prompt_type == "workflow-copilot-narration"
        return posthog_handler

    monkeypatch.setattr(narration, "get_llm_handler_for_prompt_type", _posthog_lookup)
    monkeypatch.setattr(
        narration,
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

    monkeypatch.setattr(narration, "get_llm_handler_for_prompt_type", _posthog_lookup)
    monkeypatch.setattr(
        narration,
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

    monkeypatch.setattr(narration, "get_llm_handler_for_prompt_type", _raising_lookup)
    monkeypatch.setattr(
        narration,
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

    monkeypatch.setattr(narration, "get_llm_handler_for_prompt_type", _posthog_lookup)
    monkeypatch.setattr(
        narration,
        "app",
        SimpleNamespace(WORKFLOW_COPILOT_FAST_LLM_API_HANDLER=fast, SECONDARY_LLM_API_HANDLER=object()),
    )

    handler = await narration.resolve_narrator_handler(None, "org_1")
    assert handler is fast
    assert posthog_called is False

    handler = await narration.resolve_narrator_handler("wpid_1", None)
    assert handler is fast
    assert posthog_called is False
