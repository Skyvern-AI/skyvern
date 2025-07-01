"""add_run_id_column_to_artifacts_table

Revision ID: 760ae45a1345
Revises: afeed80576cb
Create Date: 2025-06-26 14:55:09.740481+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "760ae45a1345"
down_revision: Union[str, None] = "afeed80576cb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("run_id", sa.String(), nullable=True))
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h';")
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_artifacts_run_id
            ON artifacts (run_id)
        """)
        op.execute("RESET statement_timeout;")


def downgrade() -> None:
    op.drop_column("artifacts", "run_id")
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h';")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_artifacts_run_id")
        op.execute("RESET statement_timeout;")
