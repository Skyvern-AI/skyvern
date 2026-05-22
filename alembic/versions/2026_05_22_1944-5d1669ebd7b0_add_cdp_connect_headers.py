"""add cdp_connect_headers

Revision ID: 5d1669ebd7b0
Revises: 9f512f2da31e
Create Date: 2026-05-22T19:44:51.656454+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op
import random
import time
from sqlalchemy.exc import DBAPIError

# revision identifiers, used by Alembic.
revision: str = "5d1669ebd7b0"
down_revision: Union[str, None] = "9f512f2da31e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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
