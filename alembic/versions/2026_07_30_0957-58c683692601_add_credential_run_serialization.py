"""add credential run serialization

Revision ID: 58c683692601
Revises: 1fe4efc6b752
Create Date: 2026-07-30T09:57:46.639367+00:00

"""

import random
import time
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "58c683692601"
down_revision: Union[str, None] = "1fe4efc6b752"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATEMENT_TIMEOUT = "5s"
_LOCK_TIMEOUT = "250ms"
_LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
_MIGRATION_RETRY_SECONDS = 20 * 60
_BACKOFF_BASE_SECONDS = 0.25
_BACKOFF_JITTER_SECONDS = 0.75
_ADDS = (
    ("credentials", "run_sequentially", "BOOLEAN NOT NULL DEFAULT false"),
    ("workflow_runs", "sequential_credential_id", "VARCHAR"),
)


def _is_lock_not_available(exc: DBAPIError) -> bool:
    orig = getattr(exc, "orig", None)
    return (
        getattr(orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE_SQLSTATE
        or getattr(orig, "pgcode", None) == _LOCK_NOT_AVAILABLE_SQLSTATE
    )


def _column_exists(table: str, column: str) -> bool:
    result = op.get_bind().execute(
        sa.text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = :table_name
              AND column_name = :column_name
            """
        ),
        {"table_name": table, "column_name": column},
    )
    return result.first() is not None


def _execute_transactional_schema_change(table: str, statement: str) -> None:
    try:
        op.execute("BEGIN")
        op.execute(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'")
        op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
        op.execute(f'LOCK TABLE "{table}" IN ACCESS EXCLUSIVE MODE')
        op.execute(statement)
        op.execute("COMMIT")
    except Exception as exc:
        try:
            op.execute("ROLLBACK")
        except Exception as rollback_exc:
            raise RuntimeError(
                f"rollback failed after schema change error; aborting migration: {rollback_exc}"
            ) from exc
        raise


def _execute_with_retry(table: str, statement: str, deadline: float) -> None:
    while True:
        try:
            _execute_transactional_schema_change(table, statement)
            return
        except DBAPIError as exc:
            if not _is_lock_not_available(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(_BACKOFF_BASE_SECONDS + random.random() * _BACKOFF_JITTER_SECONDS)


def upgrade() -> None:
    deadline = time.monotonic() + _MIGRATION_RETRY_SECONDS
    with op.get_context().autocommit_block():
        for table, column, definition in _ADDS:
            if not _column_exists(table, column):
                _execute_with_retry(
                    table,
                    f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS {column} {definition}',
                    deadline,
                )


def downgrade() -> None:
    deadline = time.monotonic() + _MIGRATION_RETRY_SECONDS
    with op.get_context().autocommit_block():
        for table, column, _definition in reversed(_ADDS):
            if _column_exists(table, column):
                _execute_with_retry(
                    table,
                    f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS {column}',
                    deadline,
                )
