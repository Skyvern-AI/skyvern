"""add composite index (organization_id, workflow_run_id) on script_fallback_episodes

Revision ID: d7e8f9a0b1c2
Revises: d1e2f3a4b5c6
Create Date: 2026-05-14 11:00:00.000000+00:00

"""

from typing import Sequence, Union

from alembic import op

revision: str = "d7e8f9a0b1c2"
down_revision: Union[str, None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Full composite index (not partial). Needed to support both
    # get_all_episodes_by_workflow_run_id (all rows — reviewed + unreviewed)
    # and the unreviewed-filtered convenience wrapper. A partial index on
    # WHERE reviewed=false would miss the Class A demotion review query
    # planned for the post-run v3 agent.
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
