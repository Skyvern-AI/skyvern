"""Index workflow_run_attempts.pinned_browser_session_id for browser-session folder access.

Revision ID: c27d5b17c62d
Revises: 38ab1789a9b1
Create Date: 2026-10-18T05:18:54.487452+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c27d5b17c62d"
down_revision: Union[str, None] = "38ab1789a9b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_workflow_run_attempts_pinned_session_run_lookup"


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
            "workflow_run_attempts",
            ["pinned_browser_session_id", "workflow_run_id"],
            postgresql_concurrently=True,
            postgresql_where=sa.text("pinned_browser_session_id IS NOT NULL"),
            if_not_exists=True,
        )
        op.execute("RESET statement_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(INDEX_NAME, table_name="workflow_run_attempts", postgresql_concurrently=True, if_exists=True)
