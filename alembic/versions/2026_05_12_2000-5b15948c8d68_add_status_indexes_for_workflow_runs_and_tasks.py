"""add status indexes for workflow_runs and tasks to prevent statement timeouts

Revision ID: 5b15948c8d68
Revises: 78a8db531e69
Create Date: 2026-05-12 20:00:00.000000+00:00

Mirrors the cloud-side index addition for the equivalent declarations in
skyvern/forge/sdk/db/models.py. Without this migration, alembic check fails
because the synced models reference indexes that don't exist in OSS migrations.

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b15948c8d68"
down_revision: Union[str, None] = "78a8db531e69"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h';")
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_workflow_runs_nonterminal_status
            ON workflow_runs (status, modified_at, created_at)
            WHERE status IN ('created', 'queued', 'running', 'paused')
        """)
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_tasks_nonterminal_status
            ON tasks (status, modified_at, created_at)
            WHERE status IN ('created', 'queued', 'running')
        """)
        op.execute("RESET statement_timeout;")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h';")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_tasks_nonterminal_status")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_workflow_runs_nonterminal_status")
        op.execute("RESET statement_timeout;")
