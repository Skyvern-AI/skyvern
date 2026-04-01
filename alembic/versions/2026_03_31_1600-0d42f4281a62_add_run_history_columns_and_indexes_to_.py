"""add run history columns and indexes to task_runs

Revision ID: 0d42f4281a62
Revises: d77ed2605df0
Create Date: 2026-03-31 16:00:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0d42f4281a62"
down_revision: Union[str, None] = "d77ed2605df0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Must match skyvern.schemas.runs.TERMINAL_STATUSES
TERMINAL_STATUSES = ("completed", "failed", "terminated", "canceled", "timed_out")


def upgrade() -> None:
    # Add new columns
    op.add_column("task_runs", sa.Column("status", sa.String(), nullable=True))
    op.add_column("task_runs", sa.Column("started_at", sa.DateTime(), nullable=True))
    op.add_column("task_runs", sa.Column("finished_at", sa.DateTime(), nullable=True))
    op.add_column("task_runs", sa.Column("workflow_permanent_id", sa.String(), nullable=True))
    op.add_column("task_runs", sa.Column("script_run", sa.JSON(), nullable=True))
    op.add_column("task_runs", sa.Column("parent_workflow_run_id", sa.String(), nullable=True))
    op.add_column("task_runs", sa.Column("debug_session_id", sa.String(), nullable=True))
    op.add_column("task_runs", sa.Column("searchable_text", sa.Text(), nullable=True))

    # Enable pg_trgm extension for GIN trigram index
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Add new indexes
    op.create_index(
        "ix_task_runs_org_toplevel_created",
        "task_runs",
        ["organization_id", sa.text("created_at DESC")],
        postgresql_using="btree",
        postgresql_where=sa.text("parent_workflow_run_id IS NULL AND debug_session_id IS NULL AND status IS NOT NULL"),
    )
    op.create_index(
        "ix_task_runs_org_status_created",
        "task_runs",
        ["organization_id", "status", sa.text("created_at DESC")],
        postgresql_using="btree",
    )
    op.create_index(
        "ix_task_runs_searchable_text_gin",
        "task_runs",
        ["searchable_text"],
        postgresql_using="gin",
        postgresql_ops={"searchable_text": "gin_trgm_ops"},
    )

    # Build the WHERE clause for non-terminal partial index
    status_col = sa.column("status")
    non_terminal_where = sa.or_(
        status_col.is_(None),
        ~status_col.in_(TERMINAL_STATUSES),
    )
    op.create_index(
        "ix_task_runs_nonterminal",
        "task_runs",
        ["run_id", "task_run_type"],
        postgresql_where=non_terminal_where,
    )


def downgrade() -> None:
    op.drop_index("ix_task_runs_nonterminal", table_name="task_runs")
    op.drop_index("ix_task_runs_searchable_text_gin", table_name="task_runs")
    op.drop_index("ix_task_runs_org_status_created", table_name="task_runs")
    op.drop_index("ix_task_runs_org_toplevel_created", table_name="task_runs")

    op.drop_column("task_runs", "searchable_text")
    op.drop_column("task_runs", "debug_session_id")
    op.drop_column("task_runs", "parent_workflow_run_id")
    op.drop_column("task_runs", "script_run")
    op.drop_column("task_runs", "workflow_permanent_id")
    op.drop_column("task_runs", "finished_at")
    op.drop_column("task_runs", "started_at")
    op.drop_column("task_runs", "status")
