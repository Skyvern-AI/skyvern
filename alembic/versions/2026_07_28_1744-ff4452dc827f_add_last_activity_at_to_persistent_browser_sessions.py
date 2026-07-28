"""add last_activity_at to persistent_browser_sessions

Revision ID: ff4452dc827f
Revises: 9c1f2a7be4d0
Create Date: 2026-07-28T17:44:16.437218+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ff4452dc827f"
down_revision: Union[str, None] = "9c1f2a7be4d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.add_column(
        "persistent_browser_sessions",
        sa.Column("last_activity_at", sa.DateTime(), nullable=True),
    )
    op.execute("RESET lock_timeout;")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.drop_column("persistent_browser_sessions", "last_activity_at")
    op.execute("RESET lock_timeout;")
