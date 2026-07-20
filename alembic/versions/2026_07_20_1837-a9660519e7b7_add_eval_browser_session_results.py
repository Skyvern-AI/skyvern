"""add eval browser session results

Revision ID: a9660519e7b7
Revises: e4db575f75ee
Create Date: 2026-07-20T18:37:52.068350+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9660519e7b7"
down_revision: Union[str, None] = "e4db575f75ee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "eval_browser_session_results",
        sa.Column("eval_browser_session_result_id", sa.String(), nullable=False),
        sa.Column("persistent_browser_session_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("arm", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("perfect", sa.Boolean(), nullable=True),
        sa.Column("rubric_avg", sa.Float(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("modified_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("eval_browser_session_result_id"),
        sa.UniqueConstraint(
            "persistent_browser_session_id",
            name="uq_eval_browser_session_results_persistent_browser_session_id",
        ),
    )
    op.create_index(
        "ix_eval_browser_session_results_organization_id",
        "eval_browser_session_results",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_eval_browser_session_results_organization_arm",
        "eval_browser_session_results",
        ["organization_id", "arm"],
        unique=False,
    )
    op.create_index(
        "ix_eval_browser_session_results_organization_model",
        "eval_browser_session_results",
        ["organization_id", "model"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_eval_browser_session_results_organization_model", table_name="eval_browser_session_results")
    op.drop_index("ix_eval_browser_session_results_organization_arm", table_name="eval_browser_session_results")
    op.drop_index("ix_eval_browser_session_results_organization_id", table_name="eval_browser_session_results")
    op.drop_table("eval_browser_session_results")
