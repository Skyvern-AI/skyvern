"""Index credential selections for run search.

Revision ID: 55f527c1d158
Revises: 62c6caa17362
Create Date: 2026-10-02T07:17:05.748585+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "55f527c1d158"
down_revision: Union[str, None] = "62c6caa17362"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "ix_wrcs_credential_run_lookup"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h'")
        # A cancelled CONCURRENTLY build leaves an INVALID index that IF NOT EXISTS would stamp over.
        invalid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                    "WHERE c.relname = :name AND NOT i.indisvalid"
                ),
                {"name": _INDEX_NAME},
            )
            .scalar()
        )
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
        op.create_index(
            _INDEX_NAME,
            "workflow_run_credential_selections",
            ["credential_id", "workflow_run_id"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.execute("RESET statement_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            _INDEX_NAME,
            table_name="workflow_run_credential_selections",
            postgresql_concurrently=True,
            if_exists=True,
        )
