"""add persistent_browser_sessions.claimed_at

Revision ID: 711920698281
Revises: 1cdd5c69bd07
Create Date: 2026-11-02T16:50:10.400538+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "711920698281"
down_revision: Union[str, None] = "1cdd5c69bd07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("persistent_browser_sessions", sa.Column("claimed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("persistent_browser_sessions", "claimed_at")
