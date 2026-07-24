"""add composite (workflow_permanent_id, created_at) index on workflow_runs

Revision ID: 652bfa19a63a
Revises: e4db575f75ee
Create Date: 2026-07-24T15:27:14.193148+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "652bfa19a63a"
down_revision: Union[str, None] = "e4db575f75ee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "idx_workflow_runs_wpid_created"


def upgrade() -> None:
    invalid_leftover = (
        op.get_bind()
        .execute(
            text(
                "SELECT c.oid::regclass::text FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                "WHERE c.relname = :name AND i.indrelid = 'workflow_runs'::regclass AND NOT i.indisvalid"
            ),
            {"name": INDEX_NAME},
        )
        .scalar()
    )
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s';")
        op.execute("SET statement_timeout = '3h';")
        if invalid_leftover:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {invalid_leftover};")
        op.execute(f"""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME}
            ON workflow_runs (workflow_permanent_id, created_at);
        """)
        op.execute("RESET statement_timeout;")
        op.execute("RESET lock_timeout;")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s';")
        op.execute("SET statement_timeout = '3h';")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME};")
        op.execute("RESET statement_timeout;")
        op.execute("RESET lock_timeout;")
