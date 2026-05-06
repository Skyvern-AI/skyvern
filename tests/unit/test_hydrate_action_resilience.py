"""Tests for hydrate_action resilience to malformed action rows.

Regression for SKY-9512: a single bad action row should not crash the timeline
endpoint via ValidationError propagation.
"""

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.db.utils import hydrate_action
from skyvern.webeye.actions.actions import Action, ActionStatus, ActionType, MoveAction


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
