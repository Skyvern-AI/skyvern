"""Tests for hydrate_action resilience to malformed action rows.

Regression for SKY-9512: a single bad action row should not crash the timeline
endpoint via ValidationError propagation.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skyvern.forge.sdk.db.repositories.tasks import TasksRepository
from skyvern.forge.sdk.db.utils import ACTION_TYPE_TO_CLASS, hydrate_action
from skyvern.forge.sdk.routes.routers import legacy_base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services.org_auth_service import SKYVERN_UI_USER_AGENT
from skyvern.forge.sdk.workflow.response_value_caps import RUN_RESPONSE_MAX_VALUE_BYTES
from skyvern.forge.sdk.workflow.service import redact_one_time_code_step
from skyvern.webeye.actions.actions import (
    Action,
    ActionStatus,
    ActionType,
    MoveAction,
)
from tests.unit.helpers import (
    ONE_TIME_CODE,
    assert_code_click_scrubbed,
    assert_one_time_code_withheld,
    assert_step_code_withheld,
)
from tests.unit.helpers import make_action_row as _action_row
from tests.unit.helpers import (
    make_fetched_code_action_rows,
    make_one_time_code_action_rows,
    make_one_time_code_steps,
)
from tests.unit.helpers import make_session_factory_yielding as _session_yielding


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


def test_task_actions_endpoint_serves_the_typed_value_and_withholds_the_one_time_code() -> None:
    # Driven through the route, because the defect is which repository read it calls: `text` has no
    # ActionModel column, so validating the ORM row resolves it to None on every row.
    rows = make_one_time_code_action_rows()

    from_app = _get_task_actions(*rows, headers={"x-user-agent": SKYVERN_UI_USER_AGENT})
    from_sdk = _get_task_actions(*rows)

    for response in (from_app, from_sdk):
        assert response.status_code == 200, response.text
        assert_one_time_code_withheld(response.text, response.json(), [hydrate_action(row) for row in rows])


def test_task_actions_endpoint_scrubs_a_fetched_code_from_a_click_with_no_input_row() -> None:
    response = _get_task_actions(*make_fetched_code_action_rows())

    assert response.status_code == 200, response.text
    assert ONE_TIME_CODE not in response.text
    assert_code_click_scrubbed(response.json()[1])


def test_task_steps_endpoint_withholds_the_one_time_code_and_leaves_the_stored_step_unchanged() -> None:
    steps = make_one_time_code_steps()
    repo = TasksRepository.__new__(TasksRepository)
    test_app = FastAPI()
    test_app.include_router(legacy_base_router, prefix="/api/v1")

    with (
        patch.object(TasksRepository, "Session", _session_yielding(make_one_time_code_action_rows()), create=True),
        patch.object(repo, "get_task_steps", AsyncMock(return_value=steps), create=True),
        patch("skyvern.forge.sdk.routes.agent_protocol.app") as app_module,
        patch(
            "skyvern.forge.sdk.services.org_auth_service.get_current_org_cached",
            new=AsyncMock(return_value=_organization()),
        ),
    ):
        app_module.DATABASE.tasks = repo
        response = TestClient(test_app).get("/api/v1/tasks/tsk_test/steps", headers={"x-api-key": "key"})

    assert response.status_code == 200, response.text
    assert_step_code_withheld(response.text, steps)


def test_legacy_step_keeps_its_failure_message_when_the_task_has_no_one_time_code() -> None:
    legacy = make_one_time_code_steps()[1]
    assert legacy.output is not None and legacy.output.action_results

    served = redact_one_time_code_step(legacy, set(), set())

    assert served is legacy
    assert served.output is not None and served.output.action_results[0].exception_message


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
