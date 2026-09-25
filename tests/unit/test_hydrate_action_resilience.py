"""Tests for hydrate_action resilience to malformed action rows.

Regression for SKY-9512: a single bad action row should not crash the timeline
endpoint via ValidationError propagation.
"""

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skyvern.forge.sdk.db.repositories.tasks import TasksRepository
from skyvern.forge.sdk.db.repositories.workflow_parameters import WorkflowParametersRepository
from skyvern.forge.sdk.db.utils import ACTION_TYPE_TO_CLASS, hydrate_action
from skyvern.forge.sdk.routes.routers import legacy_base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services.org_auth_service import SKYVERN_UI_USER_AGENT
from skyvern.forge.sdk.workflow.service import RUN_RESPONSE_MAX_VALUE_BYTES
from skyvern.utils.action_redaction import REDACTED_OTP_VALUE
from skyvern.webeye.actions.actions import (
    Action,
    ActionStatus,
    ActionType,
    ClickAction,
    MoveAction,
    UploadFileAction,
)
from tests.unit.helpers import make_action_row as _action_row
from tests.unit.helpers import make_session_factory_yielding as _session_yielding


def _task() -> SimpleNamespace:
    return SimpleNamespace(task_id="tsk_test", url="https://example.com", navigation_goal="goal")


def _organization() -> Organization:
    now = datetime.now(timezone.utc)
    return Organization(
        organization_id="o_test",
        organization_name="org",
        created_at=now,
        modified_at=now,
    )


def test_hydrate_action_happy_path_returns_subclass() -> None:
    row = _action_row(action_json={"x": 10, "y": 20})

    result = hydrate_action(row)

    assert isinstance(result, MoveAction)
    assert result.x == 10
    assert result.y == 20


def test_hydrate_action_falls_back_to_base_action_on_validation_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # x must be int|None. A list will not coerce, so MoveAction(...) raises ValidationError.
    row = _action_row(action_json={"x": [1, 2, 3]})

    result = hydrate_action(row)

    assert isinstance(result, Action)
    assert not isinstance(result, MoveAction)
    assert result.action_id == "act_test"
    assert result.action_type == ActionType.MOVE
    assert result.status == ActionStatus.completed
    assert result.task_id == "tsk_test"


def test_hydrate_action_unknown_action_type_falls_back_to_base_action() -> None:
    row = _action_row(action_type="not_a_real_action_type")

    result = hydrate_action(row)

    assert isinstance(result, Action)
    assert result.action_id == "act_test"


def test_every_action_type_hydrates_as_its_concrete_model() -> None:
    """SKY-12874: the mapping is exhaustive, checked against the enum at runtime.

    Asserted against ``set(ActionType)`` rather than a count, because a hardcoded number inherits
    whichever human miscounted it and keeps inheriting it. A type that falls through to base
    ``Action`` projects as ``action_model_mismatch`` and denies, so an unmapped type is fail-closed —
    but it is fail-closed by breaking every action of that type, which is worth catching here.
    """
    assert set(ACTION_TYPE_TO_CLASS) == set(ActionType)
    assert len(set(ACTION_TYPE_TO_CLASS.values())) == len(ActionType)


@pytest.mark.asyncio
async def test_subclass_fields_survive_the_production_retrieval_path() -> None:
    """SKY-12874 / AC6. Drives the repository method itself, not a hand-rolled stand-in.

    ``retrieve_action_plan`` used ``Action.model_validate(row)``, which has no action_json merge, so
    every cached action came back as a base ``Action`` with its subclass fields gone. A test that
    calls ``hydrate_action`` directly proves the helper works and says nothing about retrieval.
    """
    row = _action_row(
        action_type=ActionType.UPLOAD_FILE,
        element_id="7",
        action_json={"element_id": "7", "file_url": "https://example.com/a.pdf", "is_upload_file_tag": True},
    )
    repo = WorkflowParametersRepository.__new__(WorkflowParametersRepository)
    with patch.object(WorkflowParametersRepository, "Session", _session_yielding([row]), create=True):
        retrieved = await inspect.unwrap(WorkflowParametersRepository.retrieve_action_plan)(repo, task=_task())

    assert len(retrieved) == 1
    action = retrieved[0]
    assert isinstance(action, UploadFileAction)
    assert action.file_url == "https://example.com/a.pdf"
    assert action.is_upload_file_tag is True

    # caching.retrieve_action_plan then copies, retargets by element hash and personalizes.
    retargeted = action.model_copy()
    retargeted.element_id = "9"
    retargeted.file_url = "https://example.com/b.pdf"
    assert isinstance(retargeted, UploadFileAction)
    assert retargeted.action_type == ActionType.UPLOAD_FILE
    assert retargeted.file_url == "https://example.com/b.pdf"
    assert retargeted.is_upload_file_tag is True


@pytest.mark.asyncio
async def test_a_cached_download_click_is_retrieved_as_a_download_click() -> None:
    """The behavioural consequence of the fix above, pinned deliberately.

    ``ActionHandler.handle_action`` selects its download-capturing path with
    ``isinstance(action, ClickAction) and action.download``. A base ``Action`` failed that isinstance
    check, so a cached click recorded as a download silently skipped download capture on replay.
    This affects unenrolled runs, which is the population the security ACs give no cover for.
    """
    row = _action_row(
        action_type=ActionType.CLICK,
        element_id="3",
        action_json={"element_id": "3", "download": True},
    )
    repo = WorkflowParametersRepository.__new__(WorkflowParametersRepository)
    with patch.object(WorkflowParametersRepository, "Session", _session_yielding([row]), create=True):
        retrieved = await inspect.unwrap(WorkflowParametersRepository.retrieve_action_plan)(repo, task=_task())

    action = retrieved[0]
    assert isinstance(action, ClickAction)
    assert action.download is True


def _get_task_actions(*rows: SimpleNamespace, headers: dict[str, str] | None = None) -> Any:
    repo = TasksRepository.__new__(TasksRepository)
    test_app = FastAPI()
    test_app.include_router(legacy_base_router, prefix="/api/v1")

    with (
        patch.object(TasksRepository, "Session", _session_yielding(list(rows)), create=True),
        patch("skyvern.forge.sdk.routes.agent_protocol.app") as app_module,
        patch(
            "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
            new=AsyncMock(return_value=_organization()),
        ),
    ):
        app_module.DATABASE.tasks = repo
        return TestClient(test_app).get(
            "/api/v1/tasks/tsk_test/actions", headers={"x-api-key": "key", **(headers or {})}
        )


def test_task_actions_endpoint_serves_the_typed_value_and_the_one_time_code() -> None:
    # Driven through the route, because the defect is which repository read it calls: `text` has no
    # ActionModel column, so validating the ORM row resolves it to None on every row. A one-time code
    # is single use and the customer needs to see what was typed into the page, so it rides the wire
    # like any other typed value -- even though create_action wrote the column redacted.
    otp_row = _action_row(
        action_id="act_otp",
        action_type=ActionType.INPUT_TEXT,
        element_id="otp",
        response=REDACTED_OTP_VALUE,
        action_json={
            "element_id": "otp",
            "text": "314159",
            "response": "314159",
            "totp_identifier": "inbox@example.test",
        },
    )
    typed_row = _action_row(
        action_id="act_typed",
        action_type=ActionType.INPUT_TEXT,
        element_id="street",
        action_json={"element_id": "street", "text": "Meridian Ave"},
    )

    from_app = _get_task_actions(otp_row, typed_row, headers={"x-user-agent": SKYVERN_UI_USER_AGENT})
    from_sdk = _get_task_actions(otp_row, typed_row)

    for response in (from_app, from_sdk):
        assert response.status_code == 200, response.text
        actions = response.json()
        assert [action["text"] for action in actions] == ["314159", "Meridian Ave"]
        assert actions[0]["response"] == "314159"


def test_task_actions_endpoint_caps_for_the_app_and_keeps_the_stored_value_for_the_sdk() -> None:
    # Hydration puts a completion action's whole output on the wire, and restores the free text that
    # has no ActionModel column -- an extraction prompt runs to megabytes and the app re-reads this
    # route every five seconds. Every one of them must reach the app bounded; `get_actions` is a
    # published SDK method, so a programmatic caller must still get each one whole.
    oversized = "x" * (RUN_RESPONSE_MAX_VALUE_BYTES + 1024)
    row = _action_row(
        action_type=ActionType.COMPLETE,
        action_json={
            "output": oversized,
            "response": oversized,
            "data_extraction_goal": oversized,
            "description": oversized,
        },
    )

    from_app = _get_task_actions(row, headers={"x-user-agent": SKYVERN_UI_USER_AGENT})
    from_sdk = _get_task_actions(row)

    for field in ("output", "response", "data_extraction_goal", "description"):
        assert len(from_app.json()[0][field]) < len(oversized), field
        assert from_sdk.json()[0][field] == oversized, field
