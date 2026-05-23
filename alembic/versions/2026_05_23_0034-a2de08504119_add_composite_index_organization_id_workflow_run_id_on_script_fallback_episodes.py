"""add composite index (organization_id, workflow_run_id) on script_fallback_episodes

Revision ID: a2de08504119
Revises: 7389b537b3a4
Create Date: 2026-05-23T00:34:07.903148+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2de08504119"
down_revision: Union[str, None] = "7389b537b3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h';")
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS sfe_org_wrid_idx
            ON script_fallback_episodes (organization_id, workflow_run_id);
        """)
        op.execute("RESET statement_timeout;")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS sfe_org_wrid_idx;")
