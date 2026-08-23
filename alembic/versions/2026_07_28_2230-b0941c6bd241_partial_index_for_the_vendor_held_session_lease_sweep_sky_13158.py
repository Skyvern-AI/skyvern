"""Partial index for the vendor-held session lease sweep (SKY-13158)

Revision ID: b0941c6bd241
Revises: ff4452dc827f
Create Date: 2026-07-28T22:30:03.274576+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b0941c6bd241"
down_revision: Union[str, None] = "ff4452dc827f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "idx_pbs_vendor_held_lease"
_TABLE = "persistent_browser_sessions"
_PARTIAL_WHERE = (
    "upstream_cdp_url IS NOT NULL AND browser_address IS NULL AND completed_at IS NULL AND deleted_at IS NULL"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '1h';")
        op.create_index(
            _INDEX_NAME,
            _TABLE,
            ["last_activity_at", "started_at"],
            unique=False,
            postgresql_concurrently=True,
            postgresql_where=sa.text(_PARTIAL_WHERE),
            if_not_exists=True,
        )
        op.execute("RESET statement_timeout;")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            _INDEX_NAME,
            table_name=_TABLE,
            postgresql_concurrently=True,
            if_exists=True,
        )
