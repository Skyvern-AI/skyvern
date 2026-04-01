"""add run history columns and indexes to task_runs

Revision ID: a1b2c3d4e5f6
Revises: d77ed2605df0
Create Date: 2026-03-31 18:00:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "d77ed2605df0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new columns to task_runs
    op.add_column("task_runs", sa.Column("status", sa.String(), nullable=True))
    op.add_column("task_runs", sa.Column("started_at", sa.DateTime(), nullable=True))
    op.add_column("task_runs", sa.Column("finished_at", sa.DateTime(), nullable=True))
    op.add_column("task_runs", sa.Column("workflow_permanent_id", sa.String(), nullable=True))
    op.add_column("task_runs", sa.Column("script_run", sa.JSON(), nullable=True))
    op.add_column("task_runs", sa.Column("parent_workflow_run_id", sa.String(), nullable=True))
    op.add_column("task_runs", sa.Column("debug_session_id", sa.String(), nullable=True))
    op.add_column("task_runs", sa.Column("searchable_text", sa.Text(), nullable=True))

    # Create pg_trgm extension for GIN trigram index
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Add indexes
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
    op.create_index(
        "ix_task_runs_nonterminal",
        "task_runs",
        ["run_id", "task_run_type"],
        postgresql_where=sa.text(
            "status IS NULL OR status NOT IN ('completed', 'failed', 'terminated', 'canceled', 'timed_out')"
        ),
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
