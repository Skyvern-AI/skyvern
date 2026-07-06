from datetime import datetime
from typing import Any

import pytest

from skyvern.forge.sdk.db.models import TaskModel
from skyvern.forge.sdk.db.utils import convert_to_task


def _task_model(extracted_information: Any) -> TaskModel:
    return TaskModel(
        task_id="tsk_123",
        organization_id="o_123",
        status="completed",
        url="https://www.example.com",
        extracted_information=extracted_information,
        errors=[],
        include_extracted_text=True,
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
    )


@pytest.mark.parametrize(
    "stored_value,expected",
    [
        (True, "true"),
        (False, "false"),
        (42, "42"),
        (3.14, "3.14"),
    ],
)
def test_convert_to_task_coerces_scalar_extracted_information(stored_value: Any, expected: str) -> None:
    task = convert_to_task(_task_model(stored_value))
    assert task.extracted_information == expected


@pytest.mark.parametrize(
    "stored_value",
    [
        {"price": "$100"},
        [{"name": "item1"}, {"name": "item2"}],
        "plain string output",
        None,
    ],
)
def test_convert_to_task_preserves_json_extracted_information(stored_value: Any) -> None:
    task = convert_to_task(_task_model(stored_value))
    assert task.extracted_information == stored_value


def test_one_scalar_row_does_not_break_task_listing() -> None:
    # Mirrors the list comprehension in TasksRepository.get_tasks_by_workflow_run_id.
    rows = [_task_model({"price": "$100"}), _task_model(True), _task_model("done")]
    tasks = [convert_to_task(row) for row in rows]
    assert [task.extracted_information for task in tasks] == [{"price": "$100"}, "true", "done"]
