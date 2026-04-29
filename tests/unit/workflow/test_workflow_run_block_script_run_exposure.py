"""`workflow_run_blocks.script_run` surfaces on the `WorkflowRunBlock`
schema, so timeline consumers can distinguish cached-execution from
script-to-AI fallback without hitting Datadog.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from skyvern.forge.sdk.db.models import WorkflowRunBlockModel
from skyvern.forge.sdk.db.utils import convert_to_workflow_run_block
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.schemas.runs import ScriptRunResponse


def _fake_block_model(script_run_value: dict | None) -> MagicMock:
    """Build a `WorkflowRunBlockModel` stub that only carries the attrs
    `convert_to_workflow_run_block` touches. Avoids standing up a DB or
    constructing the SQLAlchemy model (which requires a session)."""
    now = datetime.utcnow()
    model = MagicMock(spec=WorkflowRunBlockModel)
    model.workflow_run_block_id = "wrb_test"
    model.workflow_run_id = "wr_test"
    model.block_workflow_run_id = None
    model.organization_id = "o_test"
    model.parent_workflow_run_block_id = None
    model.description = None
    model.block_type = "navigation"
    model.label = "nav_block"
    model.status = "completed"
    model.output = None
    model.continue_on_failure = False
    model.failure_reason = None
    model.error_codes = None
    model.engine = None
    model.task_id = None
    model.loop_values = None
    model.current_value = None
    model.current_index = None
    model.recipients = None
    model.attachments = None
    model.subject = None
    model.body = None
    model.created_at = now
    model.modified_at = now
    model.instructions = None
    model.positive_descriptor = None
    model.negative_descriptor = None
    model.executed_branch_id = None
    model.executed_branch_expression = None
    model.executed_branch_result = None
    model.executed_branch_next_block = None
    model.script_run = script_run_value
    return model


def test_schema_accepts_script_run_field() -> None:
    """WorkflowRunBlock pydantic model accepts a ScriptRunResponse for
    its new `script_run` field."""
    now = datetime.utcnow()
    block = WorkflowRunBlock(
        workflow_run_block_id="wrb_test",
        workflow_run_id="wr_test",
        organization_id="o_test",
        block_type="navigation",
        created_at=now,
        modified_at=now,
        script_run=ScriptRunResponse(ai_fallback_triggered=True),
    )
    assert block.script_run is not None
    assert block.script_run.ai_fallback_triggered is True


def test_schema_defaults_script_run_to_none() -> None:
    """Omitting the new field yields None — backward-compat for existing
    callers / serialized rows that predate this change."""
    now = datetime.utcnow()
    block = WorkflowRunBlock(
        workflow_run_block_id="wrb_test",
        workflow_run_id="wr_test",
        organization_id="o_test",
        block_type="navigation",
        created_at=now,
        modified_at=now,
    )
    assert block.script_run is None


def test_converter_passes_script_run_when_fallback_recorded() -> None:
    """The DB writer at `observer.py:~492` stores
    `{"ai_fallback_triggered": True}` in the column when a script→AI
    fallback fires for this block. Consumers hitting the timeline API
    must now see that as a populated `ScriptRunResponse`."""
    model = _fake_block_model(script_run_value={"ai_fallback_triggered": True})
    block = convert_to_workflow_run_block(model)
    assert block.script_run is not None
    assert block.script_run.ai_fallback_triggered is True


def test_converter_propagates_null_script_run() -> None:
    """Null DB column → None in the pydantic model. A block that ran
    cleanly from cache (or always-agent) never gets the column written."""
    model = _fake_block_model(script_run_value=None)
    block = convert_to_workflow_run_block(model)
    assert block.script_run is None


def test_converter_preserves_unknown_future_script_run_keys() -> None:
    """Pins our forward-compat contract: `ScriptRunResponse` ignores extra
    keys (set explicitly via `model_config = ConfigDict(extra="ignore")`).
    Block rows persisted with future-added keys still round-trip cleanly
    instead of raising — guards against the contract being flipped
    accidentally to `extra="forbid"` or `"allow"`.
    """
    model = _fake_block_model(
        script_run_value={
            "ai_fallback_triggered": False,
            "future_field": "ignored",
        },
    )
    block = convert_to_workflow_run_block(model)
    assert block.script_run is not None
    assert block.script_run.ai_fallback_triggered is False
