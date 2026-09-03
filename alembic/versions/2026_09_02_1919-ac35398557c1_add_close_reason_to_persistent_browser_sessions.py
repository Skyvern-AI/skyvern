"""add close_reason to persistent_browser_sessions

Revision ID: ac35398557c1
Revises: e29e30deaac9
Create Date: 2026-09-02T19:19:51.612058+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ac35398557c1"
down_revision: Union[str, None] = "e29e30deaac9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.add_column(
        "persistent_browser_sessions",
        sa.Column("close_reason", sa.String(), nullable=True),
    )
    op.execute("RESET lock_timeout;")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.drop_column("persistent_browser_sessions", "close_reason")
    op.execute("RESET lock_timeout;")
