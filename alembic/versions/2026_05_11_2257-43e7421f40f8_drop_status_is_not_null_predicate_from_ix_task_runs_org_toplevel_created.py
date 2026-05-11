"""drop status IS NOT NULL predicate from ix_task_runs_org_toplevel_created

Revision ID: 43e7421f40f8
Revises: 78a8db531e69
Create Date: 2026-05-11T22:57:56.158938+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "43e7421f40f8"
down_revision: Union[str, None] = "78a8db531e69"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h';")
        try:
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_runs_org_toplevel_created")
            op.execute("""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_runs_org_toplevel_created
                ON task_runs (organization_id, created_at DESC)
                WHERE parent_workflow_run_id IS NULL
                  AND debug_session_id IS NULL;
            """)
        finally:
            try:
                op.execute("RESET statement_timeout;")
            except Exception:
                pass  # Don't mask the original index op error


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h';")
        try:
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_runs_org_toplevel_created")
            op.execute("""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_runs_org_toplevel_created
                ON task_runs (organization_id, created_at DESC)
                WHERE parent_workflow_run_id IS NULL
                  AND debug_session_id IS NULL
                  AND status IS NOT NULL;
            """)
        finally:
            try:
                op.execute("RESET statement_timeout;")
            except Exception:
                pass  # Don't mask the original index op error
