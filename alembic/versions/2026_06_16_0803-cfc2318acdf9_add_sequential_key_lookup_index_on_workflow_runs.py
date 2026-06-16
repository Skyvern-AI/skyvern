"""add sequential_key lookup index on workflow_runs

Revision ID: cfc2318acdf9
Revises: 218048b68412
Create Date: 2026-06-16T08:03:01.419039+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cfc2318acdf9"
down_revision: Union[str, None] = "218048b68412"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '24h';")
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_workflow_runs_sequential_key_lookup
            ON workflow_runs (workflow_permanent_id, sequential_key, queued_at)
            WHERE status IN ('queued', 'running', 'paused') AND browser_session_id IS NULL
        """)
        op.execute("RESET statement_timeout;")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_workflow_runs_sequential_key_lookup")
