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

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from skyvern.config import Settings, settings
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.copilot_model_usage import CopilotModelUsageEvent, _emit_copilot_model_usage
from skyvern.forge.sdk.copilot import llm_config as copilot_llm_config
from skyvern.forge.sdk.copilot import tools as copilot_tools
from skyvern.forge.sdk.copilot.cost_telemetry import copilot_cost_scope, current_copilot_cost
from skyvern.forge.sdk.copilot.eval_llm_overrides import (
    admit_eval_llm_overrides,
    current_eval_llm_overrides,
    eval_llm_override_scope,
)
from skyvern.forge.sdk.routes import workflow_copilot as workflow_copilot_route
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatRequest,
    WorkflowCopilotQuestionResponseRequest,
    WorkflowCopilotSteerRequest,
    WorkflowCopilotStreamResponseUpdate,
)
from skyvern.schemas.llm import LLMConfig

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


@pytest.fixture
def eval_llm_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_EVAL_LLM_OVERRIDES_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_EVAL_LLM_OVERRIDES_ORGANIZATION_IDS", ["org_eval"])
    monkeypatch.setattr(
        LLMConfigRegistry,
        "_configs",
        {
            "EVAL_ACTOR": LLMConfig("openai/eval-actor", [], supports_vision=True, add_assistant_prefix=False),
            "EVAL_SAFETY": LLMConfig("openai/eval-safety", [], supports_vision=False, add_assistant_prefix=False),
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mapping",
    [
        {"workflow-copilot": "EVAL_ACTOR"},
        {"workflow-copilot-raw-secret-safety": "EVAL_SAFETY"},
        {"workflow-copilot": "EVAL_ACTOR", "workflow-copilot-raw-secret-safety": "EVAL_SAFETY"},
        {"workflow-copilot-fast": "EVAL_ACTOR"},
    ],
)
async def test_eval_overrides_preserve_every_unspecified_prompt_lane(
    mapping: dict[str, str], eval_llm_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    production = {
        prompt: object()
        for prompt in ("workflow-copilot", "workflow-copilot-fast", "workflow-copilot-raw-secret-safety")
    }

    async def production_handler(prompt: str, *_args: object) -> object:
        return production[prompt]

    monkeypatch.setattr(copilot_llm_config, "get_llm_handler_for_prompt_type", production_handler)
    admitted = admit_eval_llm_overrides(json.dumps(mapping), "org_eval")
    assert admitted is not None
    resolvers = {
        "workflow-copilot": copilot_llm_config.resolve_main_copilot_handler,
        "workflow-copilot-fast": copilot_llm_config.resolve_fast_copilot_handler,
        "workflow-copilot-raw-secret-safety": copilot_llm_config.resolve_raw_secret_safety_handler,
    }
    with eval_llm_override_scope(admitted, "wpid_eval"):
        for prompt, resolver in resolvers.items():
            handler = await resolver("wpid_eval", "org_eval")
            assert handler is (admitted.handlers[prompt] if prompt in mapping else production[prompt])
            assert await resolver("wpid_other", "org_eval") is production[prompt]
            assert await resolver("wpid_eval", "org_other") is production[prompt]
    assert current_eval_llm_overrides() is None
    assert (
        await copilot_llm_config.resolve_main_copilot_handler("wpid_eval", "org_eval") is production["workflow-copilot"]
    )


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        '{"workflow-copilot":"EVAL_ACTOR","workflow-copilot":"EVAL_SAFETY"}',
        '{"unrelated":"EVAL_ACTOR"}',
        '{"workflow-copilot":null}',
        '{"workflow-copilot":""}',
        '{"workflow-copilot":"UNREGISTERED"}',
        '{"workflow-copilot":"CUSTOM_LLM_other_org"}',
        '{"workflow-copilot":"https://host?key=value"}',
        " " * 2049,
    ],
)
def test_eval_overrides_reject_invalid_or_unavailable_configuration(raw: str, eval_llm_registry: None) -> None:
    with pytest.raises(HTTPException) as error:
        admit_eval_llm_overrides(raw, "org_eval")
    assert error.value.status_code == 400


def test_eval_overrides_require_server_and_org_authorization(
    eval_llm_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = '{"workflow-copilot":"EVAL_ACTOR"}'
    with pytest.raises(HTTPException) as error:
        admit_eval_llm_overrides(raw, "org_other")
    assert error.value.status_code == 403
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_EVAL_LLM_OVERRIDES_ENABLED", False)
    with pytest.raises(HTTPException) as error:
        admit_eval_llm_overrides(raw, "org_eval")
    assert error.value.status_code == 403
    assert admit_eval_llm_overrides(None, "org_other") is None
    assert admit_eval_llm_overrides("{}", "org_other") is None


@pytest.mark.asyncio
async def test_eval_override_contexts_are_isolated_and_reset_after_cancellation(eval_llm_registry: None) -> None:
    first = admit_eval_llm_overrides('{"workflow-copilot":"EVAL_ACTOR"}', "org_eval")
    second = admit_eval_llm_overrides('{"workflow-copilot":"EVAL_SAFETY"}', "org_eval")
    assert first is not None and second is not None
    entered = asyncio.Event()
    release = asyncio.Event()

    async def request() -> None:
        with eval_llm_override_scope(first, "wpid_eval"):
            entered.set()
            await release.wait()
        assert current_eval_llm_overrides() is None

    task = asyncio.create_task(request())
    await entered.wait()
    assert current_eval_llm_overrides() is None

    with eval_llm_override_scope(second, "wpid_eval"):
        handler = await copilot_llm_config.resolve_main_copilot_handler("wpid_eval", "org_eval")
        assert handler is second.handlers["workflow-copilot"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert current_eval_llm_overrides() is second
        with eval_llm_override_scope(None, "wpid_eval"):
            assert current_eval_llm_overrides() is None
        assert current_eval_llm_overrides() is second
    assert current_eval_llm_overrides() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("override_mapping", [{}, {"workflow-copilot": "EVAL_ACTOR"}])
async def test_eval_override_scope_covers_detached_stream_and_terminal_receipt(
    override_mapping: dict[str, str], eval_llm_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_EVAL_COST_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_EVAL_COST_ORGANIZATION_IDS", ["org_eval"])

    def detached_stream(request: Request, handler: Any) -> object:
        captured["handler"] = handler
        return captured

    async def stop_before_writes(**_kwargs: object) -> None:
        admitted = current_eval_llm_overrides()
        assert (dict(admitted.config_keys) if admitted else {}) == override_mapping
        assert current_copilot_cost() is not None
        started.set()
        await release.wait()
        assert current_eval_llm_overrides() is admitted
        raise asyncio.CancelledError

    monkeypatch.setattr(workflow_copilot_route.FastAPIEventSourceStream, "create", detached_stream)
    monkeypatch.setattr(workflow_copilot_route, "_get_or_create_workflow_copilot_chat", stop_before_writes)
    request = Request(
        {"type": "http", "headers": [(b"x-copilot-eval-llm-overrides", json.dumps(override_mapping).encode())]}
    )
    chat_request = WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid_eval", workflow_id="wf_eval", message="Hello", workflow_yaml=""
    )
    response = await workflow_copilot_route.workflow_copilot_chat_post(
        request, chat_request, SimpleNamespace(organization_id="org_eval")
    )
    assert response is captured
    assert current_eval_llm_overrides() is None
    sink = SimpleNamespace(send=AsyncMock(return_value=True), is_disconnected=AsyncMock(return_value=True))
    task = asyncio.create_task(captured["handler"](sink))
    await started.wait()
    assert current_eval_llm_overrides() is None
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert current_eval_llm_overrides() is None

    admitted = admit_eval_llm_overrides('{"workflow-copilot":"EVAL_ACTOR"}', "org_eval")
    assert admitted is not None
    with eval_llm_override_scope(admitted, "wpid_eval"):
        stream = workflow_copilot_route._FirstEventTimingStream(sink, 0)
        await stream.send(
            WorkflowCopilotStreamResponseUpdate(
                workflow_copilot_chat_id="chat_eval", message="Done", response_time=datetime.now(UTC)
            )
        )
    terminal = sink.send.await_args.args[0].model_dump()
    assert terminal["eval_llm_config_overrides"] == {"workflow-copilot": "EVAL_ACTOR"}
    assert terminal["eval_llm_config_overrides_sha256"] == admitted.sha256


@pytest.mark.asyncio
async def test_terminal_cost_export_requires_explicit_authority_even_with_inherited_ledger() -> None:
    sink = SimpleNamespace(send=AsyncMock(return_value=True))
    terminal = WorkflowCopilotStreamResponseUpdate(
        workflow_copilot_chat_id="chat_eval", message="Done", response_time=datetime.now(UTC)
    )
    with copilot_cost_scope():
        ordinary = workflow_copilot_route._FirstEventTimingStream(sink, 0)
        await ordinary.send(terminal)
        assert sink.send.await_args.args[0].llm_cost is None
        authorized = workflow_copilot_route._FirstEventTimingStream(sink, 0, include_eval_cost=True)
        await authorized.send(terminal)
        assert sink.send.await_args.args[0].llm_cost is not None
    assert current_copilot_cost() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["question", "steer"])
@pytest.mark.parametrize("organization_id", ["org_eval", "org_other"])
async def test_followup_safety_cost_receipts_require_evaluation_org(
    surface: str, organization_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_EVAL_COST_ENABLED", True)
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_EVAL_COST_ORGANIZATION_IDS", ["org_eval"])
    chat = SimpleNamespace(workflow_copilot_chat_id="chat_eval", workflow_permanent_id="wpid_eval")
    params = SimpleNamespace(
        get_workflow_copilot_chat_by_id=AsyncMock(return_value=chat),
        resolve_copilot_question=AsyncMock(
            side_effect=[SimpleNamespace(status="pending"), SimpleNamespace(status="resolved")]
        ),
        record_copilot_steer_message=AsyncMock(side_effect=lambda _org, _chat, _token, _id, steer: steer),
    )
    monkeypatch.setattr(
        workflow_copilot_route,
        "app",
        SimpleNamespace(DATABASE=SimpleNamespace(workflow_params=params), CACHE=SimpleNamespace(set=AsyncMock())),
    )
    monkeypatch.setattr(workflow_copilot_route, "resolve_raw_secret_safety_handler", AsyncMock(return_value=object()))

    async def screen(text: str, _handler: object, **_kwargs: object) -> SimpleNamespace:
        _emit_copilot_model_usage(
            CopilotModelUsageEvent(request_model="openai/eval-safety", cost=0.12, cost_source="litellm_estimate"),
            logger=SimpleNamespace(info=lambda *_args, **_kwargs: None),
        )
        return SimpleNamespace(status="safe", canonical_user_message=text)

    monkeypatch.setattr(workflow_copilot_route, "screen_raw_secret_safety", screen)
    request = Request({"type": "http", "headers": []})
    http_response = Response()
    organization = SimpleNamespace(organization_id=organization_id)
    if surface == "question":
        await workflow_copilot_route.workflow_copilot_question_response(
            request,
            http_response,
            WorkflowCopilotQuestionResponseRequest(
                workflow_copilot_chat_id="chat_eval", interaction_id="interaction_eval", text="Safe reply"
            ),
            organization,
        )
    else:
        await workflow_copilot_route.workflow_copilot_steer(
            request,
            http_response,
            WorkflowCopilotSteerRequest(
                workflow_copilot_chat_id="chat_eval",
                cancel_token="cancel_eval",
                steer_id="steer_eval",
                message="Safe reply",
            ),
            organization,
        )
    if organization_id == "org_eval":
        receipt = json.loads(http_response.headers["X-Copilot-Eval-LLM-Cost"])
        assert len(receipt["receipt_id"]) == 32
        assert receipt["llm_cost"]["known_cost_usd"] == 0.12
        assert receipt["llm_cost"]["observed_call_count"] == 1
    else:
        assert "X-Copilot-Eval-LLM-Cost" not in http_response.headers
    assert current_copilot_cost() is None
