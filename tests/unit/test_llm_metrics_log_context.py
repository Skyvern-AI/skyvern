from datetime import UTC, datetime

from skyvern.forge.sdk.api.llm.api_handler_factory import (
    _task_id_for_llm_metrics_log,
    _workflow_run_id_for_llm_metrics_log,
)
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.task_v2 import Thought


def _now() -> datetime:
    return datetime.now(UTC)


def _step() -> Step:
    now = _now()
    return Step(
        created_at=now,
        modified_at=now,
        task_id="tsk_abc",
        step_id="stp_abc",
        status=StepStatus.running,
        order=0,
        is_last=False,
        organization_id="o_test",
    )


def _thought(workflow_run_id: str = "wr_thought") -> Thought:
    now = _now()
    return Thought(
        observer_thought_id="th_1",
        observer_cruise_id="tsk_v2",
        organization_id="o_test",
        workflow_run_id=workflow_run_id,
        created_at=now,
        modified_at=now,
    )


def test_workflow_run_id_prefers_context() -> None:
    ctx = SkyvernContext(workflow_run_id="wr_ctx", task_id="tsk_ctx")
    thought = _thought()
    assert _workflow_run_id_for_llm_metrics_log(ctx, thought=thought) == "wr_ctx"


def test_workflow_run_id_falls_back_to_thought() -> None:
    ctx = SkyvernContext(task_id="tsk_ctx")
    thought = _thought()
    assert _workflow_run_id_for_llm_metrics_log(ctx, thought=thought) == "wr_thought"


def test_task_id_falls_back_to_step() -> None:
    ctx = SkyvernContext(workflow_run_id="wr_ctx")
    assert _task_id_for_llm_metrics_log(ctx, step=_step()) == "tsk_abc"


def test_task_id_prefers_context() -> None:
    ctx = SkyvernContext(workflow_run_id="wr_ctx", task_id="tsk_ctx")
    assert _task_id_for_llm_metrics_log(ctx, step=_step()) == "tsk_ctx"
