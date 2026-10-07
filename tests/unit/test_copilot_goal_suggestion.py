from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skyvern.forge.sdk.copilot.goal_suggestion import suggest_goal_from_code
from skyvern.forge.sdk.routes import workflow_copilot as workflow_copilot_route
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.services import org_auth_service
from tests.unit.copilot_test_helpers import install_org_secondary_llm_override


@pytest.mark.asyncio
async def test_a_secret_in_the_current_goal_never_reaches_the_suggestion_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = AsyncMock(return_value={"goal": "  The order page shows its total and currency.  "})
    install_org_secondary_llm_override(monkeypatch, handler)
    secret = "sk-live-9f8e7d6c5b4a39281706f5e4d3c2b1a0"

    goal = await suggest_goal_from_code(
        "o_test",
        label="read_total",
        code="return {'total': 1, 'currency': 'USD'}",
        current_goal=f"Read the order total. api_key = {secret}",
        parameter_keys=["order_url"],
    )

    assert handler.await_args is not None
    prompt = handler.await_args.kwargs["prompt"]
    assert secret not in prompt
    assert "'currency'" in prompt
    assert goal == "The order page shows its total and currency."


def test_an_oversized_suggestion_request_is_rejected_before_any_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_org() -> SimpleNamespace:
        return SimpleNamespace(organization_id="o_test")

    suggest = AsyncMock(return_value="unused")
    monkeypatch.setattr(workflow_copilot_route, "suggest_goal_from_code", suggest)
    fastapi_app = FastAPI()
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = fake_org
    fastapi_app.include_router(base_router, prefix="/v1")

    response = TestClient(fastapi_app).post(
        "/v1/workflow/copilot/suggest-goal",
        json={"label": "read_total", "code": "x" * 200_001, "current_goal": "", "parameter_keys": []},
    )

    assert response.status_code == 422
    suggest.assert_not_awaited()
