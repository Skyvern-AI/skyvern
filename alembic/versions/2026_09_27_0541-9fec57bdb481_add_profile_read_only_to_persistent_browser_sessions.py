"""add profile_read_only to persistent_browser_sessions

Revision ID: 9fec57bdb481
Revises: 9bde879d4fc0
Create Date: 2026-09-27T05:41:09.702434+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9fec57bdb481"
down_revision: Union[str, None] = "9bde879d4fc0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "persistent_browser_sessions",
        sa.Column("profile_read_only", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("persistent_browser_sessions", "profile_read_only")
