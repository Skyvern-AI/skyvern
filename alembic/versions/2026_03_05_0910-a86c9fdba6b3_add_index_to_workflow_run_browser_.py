"""add index to workflow_run browser_address

Revision ID: a86c9fdba6b3
Revises: 48fb938c9220
Create Date: 2026-03-05 09:10:12.848467+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a86c9fdba6b3"
down_revision: Union[str, None] = "48fb938c9220"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h';")
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_workflow_runs_browser_address
            ON workflow_runs (browser_address);
        """)
        op.execute("RESET statement_timeout;")


def downgrade() -> None:
    op.drop_index("ix_workflow_runs_browser_address", table_name="workflow_runs")
