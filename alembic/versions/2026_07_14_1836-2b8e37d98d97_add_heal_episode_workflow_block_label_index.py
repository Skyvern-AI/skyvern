"""add heal episode workflow block label index

Revision ID: 2b8e37d98d97
Revises: a0d23605c574
Create Date: 2026-07-14T18:36:13.734497+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2b8e37d98d97"
down_revision: Union[str, None] = "a0d23605c574"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '24h';")
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS he_org_wpid_block_label_index
            ON heal_episodes (organization_id, workflow_permanent_id, block_label, created_at)
        """)
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS he_org_wrid_index")
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS he_org_wrid_created_at_index
            ON heal_episodes (organization_id, workflow_run_id, created_at)
        """)
        op.execute("RESET statement_timeout;")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS he_org_wrid_created_at_index")
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS he_org_wrid_index
            ON heal_episodes (organization_id, workflow_run_id)
        """)
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS he_org_wpid_block_label_index")
