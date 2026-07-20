"""add eval runtime cost results

Revision ID: a3a29ad2ab68
Revises: e4db575f75ee
Create Date: 2026-07-20T19:00:56.302959+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3a29ad2ab68"
down_revision: Union[str, None] = "e4db575f75ee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "eval_runtime_cost_results",
        sa.Column("eval_runtime_cost_result_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("arm", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("tier", sa.String(), nullable=True),
        sa.Column("steps", sa.Integer(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("max_steps", sa.Integer(), nullable=True),
        sa.Column("screenshots", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cached_tokens", sa.BigInteger(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("reported_cost_usd", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("modified_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("eval_runtime_cost_result_id"),
        sa.UniqueConstraint(
            "organization_id",
            "model",
            "task_id",
            name="uq_eval_runtime_cost_results_org_model_task",
        ),
    )
    op.create_index(
        "ix_eval_runtime_cost_results_organization_id",
        "eval_runtime_cost_results",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_eval_runtime_cost_results_organization_model",
        "eval_runtime_cost_results",
        ["organization_id", "model"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_eval_runtime_cost_results_organization_model", table_name="eval_runtime_cost_results")
    op.drop_index("ix_eval_runtime_cost_results_organization_id", table_name="eval_runtime_cost_results")
    op.drop_table("eval_runtime_cost_results")
