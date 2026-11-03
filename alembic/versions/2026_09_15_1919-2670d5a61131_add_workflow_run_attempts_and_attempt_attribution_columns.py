"""add workflow run attempts and attempt attribution columns

Revision ID: 2670d5a61131
Revises: de8067e9e60c
Create Date: 2026-09-15T19:19:18.481168+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2670d5a61131"
down_revision: Union[str, None] = "de8067e9e60c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_run_attempts",
        sa.Column("workflow_run_id", sa.String(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("failure_category", sa.JSON(), nullable=True),
        sa.Column("error_codes", sa.JSON(), nullable=True),
        sa.Column("retry_decision", sa.String(), nullable=True),
        sa.Column("decision_reason", sa.String(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("next_attempt_prepared_at", sa.DateTime(), nullable=True),
        sa.Column("webhook_sent_at", sa.DateTime(), nullable=True),
        sa.Column("interim_webhook_sent_at", sa.DateTime(), nullable=True),
        sa.Column("side_effects_released_at", sa.DateTime(), nullable=True),
        sa.Column("pinned_browser_session_id", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("workflow_run_id", "attempt_number"),
    )
    op.create_index(
        "ix_workflow_run_attempts_organization_created_at",
        "workflow_run_attempts",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_workflow_run_attempts_pending_retries",
        "workflow_run_attempts",
        ["next_attempt_at", "workflow_run_id", "attempt_number"],
        postgresql_where=sa.text(
            "retry_decision = 'retry' AND next_attempt_prepared_at IS NULL AND next_attempt_at IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_workflow_run_attempts_terminal_releases",
        "workflow_run_attempts",
        ["side_effects_released_at"],
        postgresql_where=sa.text("retry_decision IN ('final', 'revoked', 'abandoned') AND webhook_sent_at IS NULL"),
    )
    op.create_index(
        "ix_workflow_run_attempts_prepared_not_started",
        "workflow_run_attempts",
        ["modified_at"],
        postgresql_where=sa.text("retry_decision IS NULL AND status = 'queued' AND started_at IS NULL"),
    )

    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_run_blocks", sa.Column("attempt_number", sa.Integer(), nullable=True))
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("tasks", sa.Column("attempt_number", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("tasks", "attempt_number")
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_run_blocks", "attempt_number")
    op.drop_index("ix_workflow_run_attempts_prepared_not_started", table_name="workflow_run_attempts")
    op.drop_index("ix_workflow_run_attempts_terminal_releases", table_name="workflow_run_attempts")
    op.drop_index("ix_workflow_run_attempts_pending_retries", table_name="workflow_run_attempts")
    op.drop_index("ix_workflow_run_attempts_organization_created_at", table_name="workflow_run_attempts")
    op.drop_table("workflow_run_attempts")
