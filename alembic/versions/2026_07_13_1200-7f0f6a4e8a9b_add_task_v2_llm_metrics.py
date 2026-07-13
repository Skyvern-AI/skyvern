"""add durable Task V2 LLM and run metrics

Revision ID: 7f0f6a4e8a9b
Revises: a0d23605c574
Create Date: 2026-07-13 12:00:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op


revision: str = "7f0f6a4e8a9b"
down_revision: Union[str, None] = "a0d23605c574"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_v2_run_metrics",
        sa.Column("task_v2_run_metrics_id", sa.String(), nullable=False),
        sa.Column("task_v2_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("workflow_run_id", sa.String(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=True),
        sa.Column("workflow_permanent_id", sa.String(), nullable=True),
        sa.Column("iteration_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("loop_item_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("task_v2_run_metrics_id"),
        sa.UniqueConstraint("workflow_run_id"),
    )
    op.create_index(
        "t2rm_org_created_at_index",
        "task_v2_run_metrics",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "t2rm_org_wfr_index",
        "task_v2_run_metrics",
        ["organization_id", "workflow_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_task_v2_run_metrics_task_v2_id",
        "task_v2_run_metrics",
        ["task_v2_id"],
        unique=False,
    )

    op.create_table(
        "task_v2_llm_calls",
        sa.Column("task_v2_llm_call_id", sa.String(), nullable=False),
        sa.Column("task_v2_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("workflow_run_id", sa.String(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=True),
        sa.Column("workflow_permanent_id", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("step_id", sa.String(), nullable=True),
        sa.Column("thought_id", sa.String(), nullable=True),
        sa.Column("workflow_run_block_id", sa.String(), nullable=True),
        sa.Column("call_type", sa.String(), nullable=False),
        sa.Column("prompt_name", sa.String(), nullable=False),
        sa.Column("task_type", sa.String(), nullable=True),
        sa.Column("iteration", sa.Integer(), nullable=True),
        sa.Column("loop_item_count", sa.Integer(), nullable=True),
        sa.Column("loop_index", sa.Integer(), nullable=True),
        sa.Column("retry_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_speculative", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("llm_key", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("requested_model", sa.String(), nullable=True),
        sa.Column("provider_request_id", sa.String(), nullable=True),
        sa.Column("input_token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reasoning_token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("image_token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cached_token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("llm_cost", sa.Numeric(), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'completed'"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("task_v2_llm_call_id"),
    )
    op.create_index(
        "t2llm_org_created_at_index",
        "task_v2_llm_calls",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "t2llm_org_wfr_index",
        "task_v2_llm_calls",
        ["organization_id", "workflow_run_id"],
        unique=False,
    )
    op.create_index(
        "t2llm_wfr_prompt_index",
        "task_v2_llm_calls",
        ["workflow_run_id", "prompt_name"],
        unique=False,
    )
    op.create_index(
        "t2llm_wfr_model_index",
        "task_v2_llm_calls",
        ["workflow_run_id", "model"],
        unique=False,
    )
    op.create_index(
        "t2llm_wfr_provider_request_index",
        "task_v2_llm_calls",
        ["workflow_run_id", "provider_request_id"],
        unique=False,
    )
    op.create_index(
        "ix_task_v2_llm_calls_task_v2_id",
        "task_v2_llm_calls",
        ["task_v2_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_v2_llm_calls_task_v2_id", table_name="task_v2_llm_calls")
    op.drop_index("t2llm_wfr_provider_request_index", table_name="task_v2_llm_calls")
    op.drop_index("t2llm_wfr_model_index", table_name="task_v2_llm_calls")
    op.drop_index("t2llm_wfr_prompt_index", table_name="task_v2_llm_calls")
    op.drop_index("t2llm_org_wfr_index", table_name="task_v2_llm_calls")
    op.drop_index("t2llm_org_created_at_index", table_name="task_v2_llm_calls")
    op.drop_table("task_v2_llm_calls")

    op.drop_index("ix_task_v2_run_metrics_task_v2_id", table_name="task_v2_run_metrics")
    op.drop_index("t2rm_org_wfr_index", table_name="task_v2_run_metrics")
    op.drop_index("t2rm_org_created_at_index", table_name="task_v2_run_metrics")
    op.drop_table("task_v2_run_metrics")
