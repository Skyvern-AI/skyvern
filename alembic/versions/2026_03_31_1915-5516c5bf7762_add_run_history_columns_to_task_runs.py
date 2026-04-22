"""add run history columns to task_runs

Revision ID: 5516c5bf7762
Revises: d77ed2605df0
Create Date: 2026-03-31T19:15:14.171986+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5516c5bf7762"
down_revision: Union[str, None] = "d77ed2605df0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("task_runs", sa.Column("status", sa.String(), nullable=True))
    op.add_column("task_runs", sa.Column("started_at", sa.DateTime(), nullable=True))
    op.add_column("task_runs", sa.Column("finished_at", sa.DateTime(), nullable=True))
    op.add_column("task_runs", sa.Column("workflow_permanent_id", sa.String(), nullable=True))
    op.add_column("task_runs", sa.Column("script_run", sa.JSON(), nullable=True))
    op.add_column("task_runs", sa.Column("parent_workflow_run_id", sa.String(), nullable=True))
    op.add_column("task_runs", sa.Column("debug_session_id", sa.String(), nullable=True))
    op.add_column("task_runs", sa.Column("searchable_text", sa.Text(), nullable=True))
    # pg_trgm is available by default on RDS and Cloud SQL.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # All indexes are created CONCURRENTLY to avoid SHARE locks that block writes
    # on the actively-written task_runs table.
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h';")
        try:
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_runs_org_status_created "
                "ON task_runs USING btree (organization_id, status, created_at DESC)"
            )
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_runs_searchable_text_gin "
                "ON task_runs USING gin (searchable_text gin_trgm_ops)"
            )
            op.execute("""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_runs_org_toplevel_created
                ON task_runs (organization_id, created_at DESC)
                WHERE parent_workflow_run_id IS NULL
                  AND debug_session_id IS NULL
                  AND status IS NOT NULL;
            """)
            # Partial index covering non-terminal task_runs rows.
            # Used by the task_runs_sync_activity cron to efficiently find rows
            # that still need syncing. The index shrinks as runs complete.
            op.execute("""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_runs_nonterminal
                ON task_runs (run_id, task_run_type)
                WHERE status IS NULL
                   OR status NOT IN ('completed', 'failed', 'terminated', 'canceled', 'timed_out')
            """)
        finally:
            try:
                op.execute("RESET statement_timeout;")
            except Exception:
                pass  # Don't mask the original index creation error


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_task_runs_nonterminal")
    op.execute("DROP INDEX IF EXISTS ix_task_runs_org_toplevel_created")
    op.execute("DROP INDEX IF EXISTS ix_task_runs_searchable_text_gin")
    op.execute("DROP INDEX IF EXISTS ix_task_runs_org_status_created")
    op.drop_column("task_runs", "searchable_text")
    op.drop_column("task_runs", "debug_session_id")
    op.drop_column("task_runs", "parent_workflow_run_id")
    op.drop_column("task_runs", "script_run")
    op.drop_column("task_runs", "workflow_permanent_id")
    op.drop_column("task_runs", "finished_at")
    op.drop_column("task_runs", "started_at")
    op.drop_column("task_runs", "status")
