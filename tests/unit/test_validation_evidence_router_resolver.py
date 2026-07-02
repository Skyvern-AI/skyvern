"""Tests for ``ForgeAgent.resolve_validation_evidence_route`` (SKY-10620).

The resolver intentionally has a single runtime feature flag:
``VALIDATION_EVIDENCE_ROUTER_MODE``. The confidence floor is a code constant,
not a second remotely tunable flag, so rollout remains simple while the safety
threshold stays conservative and reviewable in code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import agent as agent_module
from skyvern.forge import app
from skyvern.forge.agent import (
    DEFAULT_VALIDATION_ROUTER_MIN_CONFIDENCE,
    resolve_validation_evidence_route,
)
from skyvern.forge.validation_evidence_router import (
    ValidationRouterDecision,
    ValidationRouterMode,
    ValidationRouterResult,
)
from tests.unit.helpers import make_organization, make_task


async def _capture_router_call(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode_value: Any,
) -> tuple[dict[str, Any], AsyncMock]:
    captured: dict[str, Any] = {}

    async def get_value_cached(*_args: Any, **_kwargs: Any) -> Any:
        return mode_value

    payload_calls = AsyncMock(return_value=0.5)

    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "get_value_cached", get_value_cached)
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "get_payload_cached", payload_calls)

    async def fake_route(**kwargs: Any) -> ValidationRouterResult:
        captured.update(kwargs)
        return ValidationRouterResult(
            effective_without_page_information=False,
            decision=ValidationRouterDecision.PAGE_AWARE,
            mode=kwargs["mode"],
        )

    monkeypatch.setattr(agent_module, "route_validation_evidence", fake_route)

    now = datetime.now(UTC)
    org = make_organization(now)
    task = make_task(now, org, organization_id=org.organization_id, workflow_run_id="wr_test")

    await resolve_validation_evidence_route(
        task=task,
        step=None,
        complete_criterion="x",
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
    )
    return captured, payload_calls


@pytest.mark.asyncio
async def test_resolver_uses_code_constant_confidence_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    captured, payload_calls = await _capture_router_call(monkeypatch, mode_value="enforce")
    assert captured.get("mode") is ValidationRouterMode.ENFORCE
    assert captured.get("min_confidence") == DEFAULT_VALIDATION_ROUTER_MIN_CONFIDENCE
    payload_calls.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode_value", "expected_mode"),
    [
        ("off", ValidationRouterMode.OFF),
        ("shadow", ValidationRouterMode.SHADOW),
        ("enforce", ValidationRouterMode.ENFORCE),
        (" ENFORCE ", ValidationRouterMode.ENFORCE),
        ("unknown", ValidationRouterMode.OFF),
        (None, ValidationRouterMode.OFF),
    ],
)
async def test_resolver_parses_only_mode_flag(
    monkeypatch: pytest.MonkeyPatch,
    mode_value: Any,
    expected_mode: ValidationRouterMode,
) -> None:
    captured, payload_calls = await _capture_router_call(monkeypatch, mode_value=mode_value)
    assert captured.get("mode") is expected_mode
    assert captured.get("min_confidence") == DEFAULT_VALIDATION_ROUTER_MIN_CONFIDENCE
    payload_calls.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolver_mode_read_failure_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get_value_cached(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("posthog down")

    payload_calls = AsyncMock(return_value=0.1)
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "get_value_cached", get_value_cached)
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "get_payload_cached", payload_calls)

    captured: dict[str, Any] = {}

    async def fake_route(**kwargs: Any) -> ValidationRouterResult:
        captured.update(kwargs)
        return ValidationRouterResult(
            effective_without_page_information=False,
            decision=ValidationRouterDecision.PAGE_AWARE,
            mode=kwargs["mode"],
        )

    monkeypatch.setattr(agent_module, "route_validation_evidence", fake_route)

    now = datetime.now(UTC)
    org = make_organization(now)
    task = make_task(now, org, organization_id=org.organization_id, workflow_run_id="wr_test")

    await resolve_validation_evidence_route(
        task=task,
        step=None,
        complete_criterion="x",
        terminate_criterion=None,
        navigation_payload_str="{}",
        error_code_mapping_str=None,
        local_datetime="2026-06-08T00:00:00",
    )

    assert captured.get("mode") is ValidationRouterMode.OFF
    assert captured.get("min_confidence") == DEFAULT_VALIDATION_ROUTER_MIN_CONFIDENCE
    payload_calls.assert_not_awaited()
