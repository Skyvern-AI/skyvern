"""Shared constructors for the Task V3 block prompt-assembly tests (goal composition, workflow
position, handoff redaction)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow.models.block import TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.workflows import BlockType

# Known-signed example (mirrors tests/unit/test_taskv3_opaque_refs.py::SIGNED): a JWT-shaped token
# value under an allowlisted "token" key, which is_signed_url() flags as a signing artifact.
SIGNED_URL = (
    "https://files.example.test/uploads/a1b2c3d4e5f6/resume.pdf"
    "?token=eyJhbGciOiJIUzI1NiJ9.c2lnbmVk.Q29ycmVjdEhvcnNlQmF0dGVyeVN0YXBsZTAxMjM0NTY3ODk"
)
PLAIN_URL = "https://portfolio.example.test/jobs/12345"


def output_param(key: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        description="test output",
        output_parameter_id=f"op_{key}",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )


def make_block(label: str = "blk") -> TaskBlock:
    return TaskBlock(label=label, output_parameter=output_param(label))


def run_block(**overrides: Any) -> WorkflowRunBlock:
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "workflow_run_block_id": "wrb_1",
        "workflow_run_id": "wr_1",
        "organization_id": "org-123",
        "block_type": BlockType.TASK,
        "created_at": now,
        "modified_at": now,
    }
    base.update(overrides)
    return WorkflowRunBlock(**base)


def make_workflow_run_context(blocks: list) -> MagicMock:
    ctx = MagicMock()
    ctx.workflow.workflow_definition.blocks = blocks
    # A bare MagicMock attribute is truthy; a real definition has no finally block unless set.
    ctx.workflow.workflow_definition.finally_block_label = None
    return ctx
