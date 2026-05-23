"""add cdp_connect_headers

Revision ID: 0b0bd1875c6e
Revises: 9f512f2da31e
Create Date: 2026-05-22T20:59:01.986004+00:00

ACCESS EXCLUSIVE on the target tables conflicts with every other lock
mode, so a queued ALTER can block concurrent queries behind it. Each
ALTER first tries to take an ACCESS EXCLUSIVE lock with a short
lock_timeout inside its own transaction; failed attempts roll back
immediately and retry outside the lock queue.
"""

import random
import time
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0b0bd1875c6e"
down_revision: Union[str, None] = "9f512f2da31e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("observer_cruises", "tasks", "workflow_runs", "workflows")
_COLUMN = "cdp_connect_headers"
_STATEMENT_TIMEOUT = "5s"
_LOCK_TIMEOUT = "250ms"
_LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
_MIGRATION_RETRY_SECONDS = 20 * 60
_BACKOFF_BASE_SECONDS = 0.25
_BACKOFF_JITTER_SECONDS = 0.75


def _is_lock_not_available(exc: DBAPIError) -> bool:
    orig = getattr(exc, "orig", None)
    return (
        getattr(orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE_SQLSTATE
        or getattr(orig, "pgcode", None) == _LOCK_NOT_AVAILABLE_SQLSTATE
    )


def _column_exists(table: str) -> bool:
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
        {"table_name": table, "column_name": _COLUMN},
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
        for table in reversed(_TABLES):
            if not _column_exists(table):
                _execute_with_retry(
                    table,
                    f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS {_COLUMN} JSON',
                    deadline,
                )


def downgrade() -> None:
    deadline = time.monotonic() + _MIGRATION_RETRY_SECONDS
    with op.get_context().autocommit_block():
        for table in _TABLES:
            if _column_exists(table):
                _execute_with_retry(table, f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS {_COLUMN}', deadline)
