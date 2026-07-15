"""add VNC metadata to persistent browser sessions

Revision ID: d4f7a9c2e681
Revises: 1915b0e1126e
Create Date: 2026-07-13 18:18:10.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4f7a9c2e681"
down_revision: Union[str, None] = "1915b0e1126e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE_NAME = "persistent_browser_sessions"
_LOCK_TIMEOUT = "5s"


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    # Nullable adds, including the constant interactor default, are metadata-only on PostgreSQL 11+;
    # no explicit table-rewrite/backfill UPDATE is needed.
    op.add_column(_TABLE_NAME, sa.Column("display_number", sa.Integer(), nullable=True))
    op.add_column(_TABLE_NAME, sa.Column("vnc_port", sa.Integer(), nullable=True))
    op.add_column(
        _TABLE_NAME,
        sa.Column("interactor", sa.String(), server_default=sa.text("'agent'"), nullable=True),
    )


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    op.drop_column(_TABLE_NAME, "interactor")
    op.drop_column(_TABLE_NAME, "vnc_port")
    op.drop_column(_TABLE_NAME, "display_number")
