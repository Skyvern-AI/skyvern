"""Tests for hydrate_action resilience to malformed action rows.

Regression for SKY-9512: a single bad action row should not crash the timeline
endpoint via ValidationError propagation.
"""

import inspect
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from skyvern.forge.sdk.db.repositories.workflow_parameters import WorkflowParametersRepository
from skyvern.forge.sdk.db.utils import ACTION_TYPE_TO_CLASS, hydrate_action
from skyvern.webeye.actions.actions import (
    Action,
    ActionStatus,
    ActionType,
    ClickAction,
    MoveAction,
    UploadFileAction,
)


def _task() -> SimpleNamespace:
    return SimpleNamespace(task_id="tsk_test", url="https://example.com", navigation_goal="goal")


def _session_yielding(rows: list) -> Any:
    """A Session factory whose scalars() returns `rows`, so the repository method runs for real."""

    class _Result:
        @staticmethod
        def all() -> list:
            return rows

    class _Session:
        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def scalars(self, _query: Any) -> _Result:
            return _Result()

    return staticmethod(lambda *_args: _Session())


def _action_row(**overrides: Any) -> SimpleNamespace:
    """Build a minimal duck-typed stand-in for ActionModel."""
    base: dict[str, Any] = {
        "action_id": "act_test",
        "action_type": ActionType.MOVE,
        "status": ActionStatus.completed,
        "source_action_id": None,
        "organization_id": "o_test",
        "workflow_run_id": "wr_test",
        "task_id": "tsk_test",
        "step_id": "stp_test",
        "step_order": 0,
        "action_order": 0,
        "confidence_float": None,
        "reasoning": None,
        "intention": None,
        "response": None,
        "element_id": None,
        "skyvern_element_hash": None,
        "skyvern_element_data": None,
        "screenshot_artifact_id": None,
        "created_at": datetime(2026, 5, 6, 0, 0, 0),
        "modified_at": datetime(2026, 5, 6, 0, 0, 0),
        "action_json": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


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
