"""add cdp routing columns to persistent browser sessions

Revision ID: f2bad0d757f9
Revises: d9ab2c4d41e5
Create Date: 2026-07-18T05:14:50.640295+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2bad0d757f9"
down_revision: Union[str, None] = "d9ab2c4d41e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("persistent_browser_sessions", sa.Column("upstream_cdp_url", sa.String(), nullable=True))
    op.add_column("persistent_browser_sessions", sa.Column("browser_vendor", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("persistent_browser_sessions", "browser_vendor")
    op.drop_column("persistent_browser_sessions", "upstream_cdp_url")
