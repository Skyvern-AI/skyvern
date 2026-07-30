"""add credential gate indexes

Revision ID: 64cbada89956
Revises: 58c683692601
Create Date: 2026-07-30T09:57:46.639871+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "64cbada89956"
down_revision: Union[str, None] = "58c683692601"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h';")
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_workflow_runs_sequential_credential_gate
            ON workflow_runs (organization_id, sequential_credential_id, queued_at)
            WHERE sequential_credential_id IS NOT NULL
              AND status IN ('queued', 'running', 'paused');
        """)
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_workflow_runs_serialized_ticket
            ON workflow_runs (organization_id, queued_at DESC)
            WHERE status IN ('queued', 'running', 'paused');
        """)
        op.execute("RESET statement_timeout;")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_workflow_runs_serialized_ticket;")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_workflow_runs_sequential_credential_gate;")
