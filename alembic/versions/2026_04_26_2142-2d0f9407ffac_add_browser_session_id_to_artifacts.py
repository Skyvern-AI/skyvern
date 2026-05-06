"""add browser_session_id to artifacts

Revision ID: 2d0f9407ffac
Revises: bd362c15b74b
Create Date: 2026-04-26T21:42:34.513980+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2d0f9407ffac"
down_revision: Union[str, None] = "bd362c15b74b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("browser_session_id", sa.String(), nullable=True))

    # Partial index build on a very large table. CREATE INDEX CONCURRENTLY
    # avoids the long-held ACCESS EXCLUSIVE lock plain CREATE INDEX would take;
    # statement_timeout gives the build room to finish on production volumes.
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h';")
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_artifacts_browser_session_id_partial
            ON artifacts (browser_session_id)
            WHERE browser_session_id IS NOT NULL
        """)
        op.execute("RESET statement_timeout;")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h';")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_artifacts_browser_session_id_partial")
        op.execute("RESET statement_timeout;")
    op.drop_column("artifacts", "browser_session_id")
