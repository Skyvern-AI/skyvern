"""add close_requested_at to persistent_browser_sessions

Revision ID: 22705e03c606
Revises: ceb0b2b5836b
Create Date: 2026-08-18T16:36:02.886582+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "22705e03c606"
down_revision: Union[str, None] = "ceb0b2b5836b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.add_column(
        "persistent_browser_sessions",
        sa.Column("close_requested_at", sa.DateTime(), nullable=True),
    )
    op.execute("RESET lock_timeout;")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.drop_column("persistent_browser_sessions", "close_requested_at")
    op.execute("RESET lock_timeout;")
