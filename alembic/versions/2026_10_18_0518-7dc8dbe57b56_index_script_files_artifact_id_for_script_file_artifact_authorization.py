"""Index script_files.artifact_id for script-file artifact authorization.

Revision ID: 7dc8dbe57b56
Revises: 74aa11c2ce07
Create Date: 2026-10-18T05:18:54.484396+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7dc8dbe57b56"
down_revision: Union[str, None] = "74aa11c2ce07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_script_files_artifact_id"


def _drop_invalid_index() -> None:
    # A cancelled CONCURRENTLY build leaves an INVALID index that IF NOT EXISTS would stamp over.
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                "WHERE c.relname = :name AND NOT i.indisvalid"
            ),
            {"name": INDEX_NAME},
        )
        .scalar()
    )
    if invalid:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h'")
        _drop_invalid_index()
        op.create_index(
            INDEX_NAME,
            "script_files",
            ["artifact_id"],
            postgresql_concurrently=True,
            postgresql_where=sa.text("artifact_id IS NOT NULL"),
            if_not_exists=True,
        )
        op.execute("RESET statement_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(INDEX_NAME, table_name="script_files", postgresql_concurrently=True, if_exists=True)
