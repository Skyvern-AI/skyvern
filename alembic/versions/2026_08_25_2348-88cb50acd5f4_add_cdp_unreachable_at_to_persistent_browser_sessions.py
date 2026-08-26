"""add cdp_unreachable_at to persistent_browser_sessions

Revision ID: 88cb50acd5f4
Revises: bb05ff91073f
Create Date: 2026-08-25T23:48:03.104718+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "88cb50acd5f4"
down_revision: str | None = "bb05ff91073f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("persistent_browser_sessions", sa.Column("cdp_unreachable_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("persistent_browser_sessions", "cdp_unreachable_at")
