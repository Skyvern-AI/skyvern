"""max_screenshot_scrolls is clamped (not rejected) into [0, MAX_SCREENSHOT_SCROLLS].

An unbounded value larger than the INTEGER column limit overflowed
max_screenshot_scrolling_times on insert (psycopg NumericValueOutOfRange). A
before-validator clamps the value, which fixes the write overflow while staying
safe on the read path: the same field is populated when hydrating persisted rows
(Task from convert_to_task, the workflow-run response's run_request), so a
pre-cap historical value must clamp rather than raise.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import BaseModel

from skyvern.constants import MAX_SCREENSHOT_SCROLLS
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Request
from skyvern.forge.sdk.schemas.tasks import Task, TaskRequest
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody
from skyvern.schemas.run_blocks import BaseRunBlockRequest
from skyvern.schemas.runs import TaskRunRequest, WorkflowRunRequest
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest

_PG_INT_OVERFLOW = 2**31

# (model, field, required-kwargs) for every request/persistence model carrying the field.
_CASES = [
    (TaskRunRequest, "max_screenshot_scrolls", {"prompt": "x"}),
    (WorkflowRunRequest, "max_screenshot_scrolls", {"workflow_id": "wpid_1"}),
    (
        WorkflowCreateYAMLRequest,
        "max_screenshot_scrolls",
        {"title": "t", "workflow_definition": {"parameters": [], "blocks": []}},
    ),
    (BaseRunBlockRequest, "max_screenshot_scrolling_times", {}),
    (WorkflowRequestBody, "max_screenshot_scrolls", {}),
    (TaskRequest, "max_screenshot_scrolls", {"url": "https://example.com"}),
    (TaskV2Request, "max_screenshot_scrolls", {"user_prompt": "x"}),
]


@pytest.mark.parametrize(("model", "field", "required"), _CASES)
def test_overflowing_scroll_count_is_clamped_not_rejected(model: type[BaseModel], field: str, required: dict) -> None:
    instance = model(**required, **{field: _PG_INT_OVERFLOW})
    assert getattr(instance, field) == MAX_SCREENSHOT_SCROLLS


@pytest.mark.parametrize(("model", "field", "required"), _CASES)
def test_stringified_overflow_is_clamped_after_coercion(model: type[BaseModel], field: str, required: dict) -> None:
    # A quoted number is coerced to int by pydantic; the clamp must run after
    # coercion, or a stringified overflow slips past and re-overflows on insert.
    instance = model(**required, **{field: str(_PG_INT_OVERFLOW)})
    assert getattr(instance, field) == MAX_SCREENSHOT_SCROLLS


@pytest.mark.parametrize(("model", "field", "required"), _CASES)
def test_negative_scroll_count_is_clamped_to_zero(model: type[BaseModel], field: str, required: dict) -> None:
    assert getattr(model(**required, **{field: -1}), field) == 0


@pytest.mark.parametrize(("model", "field", "required"), _CASES)
def test_in_range_and_none_scroll_counts_preserved(model: type[BaseModel], field: str, required: dict) -> None:
    assert getattr(model(**required, **{field: 5}), field) == 5
    assert getattr(model(**required, **{field: MAX_SCREENSHOT_SCROLLS}), field) == MAX_SCREENSHOT_SCROLLS
    assert getattr(model(**required), field) is None


def test_hydrating_historical_over_cap_task_row_does_not_raise() -> None:
    """A persisted Task row with a pre-cap scroll count must clamp on read, not 500.

    Task inherits TaskBase and is built from the DB by convert_to_task; rejecting
    here would break task detail/list for historical rows.
    """
    now = datetime.now(timezone.utc)
    task = Task(
        task_id="tsk_1",
        organization_id="o_1",
        status="completed",
        created_at=now,
        modified_at=now,
        url="https://example.com",
        max_screenshot_scrolls=50_000,
    )
    assert task.max_screenshot_scrolls == MAX_SCREENSHOT_SCROLLS


def test_reconstructing_workflow_run_request_over_cap_does_not_raise() -> None:
    """The workflow-run response builder reconstructs WorkflowRunRequest from the
    stored run; a historical over-cap value must clamp rather than raise."""
    reconstructed = WorkflowRunRequest(workflow_id="wpid_1", max_screenshot_scrolls=50_000)
    assert reconstructed.max_screenshot_scrolls == MAX_SCREENSHOT_SCROLLS
