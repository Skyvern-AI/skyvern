"""add tasks standalone status index

Revision ID: 1cf1687463a4
Revises: aee363385de8
Create Date: 2026-10-18T05:18:54.480132+00:00

"""

from typing import Sequence, Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1cf1687463a4"
down_revision: Union[str, None] = "aee363385de8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_tasks_org_standalone_status_created"
INDEX_DDL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
    "ON tasks (organization_id, status, created_at) WHERE workflow_run_id IS NULL"
)
INDEX_DEFINITION = "USING btree (organization_id, status, created_at) WHERE (workflow_run_id IS NULL)"
INDEX_STATE_SQL = (
    "SELECT i.indisvalid, pg_get_indexdef(c.oid) FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "WHERE c.relname = :name AND i.indrelid = 'tasks'::regclass"
)
LEFTOVER_DROP_LOCK_TIMEOUT = "5s"


def upgrade() -> None:
    is_valid, definition = op.get_bind().execute(text(INDEX_STATE_SQL), {"name": INDEX_NAME}).first() or (None, "")
    # Even a CREATE INDEX IF NOT EXISTS that finds the index first waits for SHARE UPDATE EXCLUSIVE on tasks,
    # which a running autovacuum holds. Where the index was built out of band, the deploy must issue no DDL at all.
    if is_valid:
        if not (definition.startswith(f"CREATE INDEX {INDEX_NAME} ON ") and definition.endswith(INDEX_DEFINITION)):
            raise RuntimeError(
                f"{INDEX_NAME} is valid but its definition is {definition!r}, not {INDEX_DEFINITION!r}. "
                "Drop it with DROP INDEX CONCURRENTLY and rebuild it as INDEX_DDL does before upgrading."
            )
        return
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h';")
        if is_valid is False:
            # An INVALID index is a cancelled CONCURRENTLY build, or one still running out of band. The latter holds
            # SHARE UPDATE EXCLUSIVE on tasks until it turns valid, so fail fast instead of waiting to drop its index.
            op.execute(f"SET lock_timeout = '{LEFTOVER_DROP_LOCK_TIMEOUT}';")
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME};")
        # No short lock_timeout for the build: CIC's lock waits do not block writers, and a timeout in its later
        # phases fails the build after both heap scans and leaves an INVALID index.
        op.execute("SET lock_timeout = 0;")
        op.execute(INDEX_DDL)
        op.execute("RESET statement_timeout;")
        op.execute("RESET lock_timeout;")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s';")
        op.execute("SET statement_timeout = '3h';")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME};")
        op.execute("RESET statement_timeout;")
        op.execute("RESET lock_timeout;")
