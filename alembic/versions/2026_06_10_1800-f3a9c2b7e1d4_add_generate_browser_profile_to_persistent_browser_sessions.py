"""add generate_browser_profile to persistent_browser_sessions

Revision ID: f3a9c2b7e1d4
Revises: 4dbd183edee0
Create Date: 2026-06-10T18:00:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a9c2b7e1d4"
down_revision: Union[str, None] = "4dbd183edee0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "persistent_browser_sessions",
        sa.Column("generate_browser_profile", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("persistent_browser_sessions", "generate_browser_profile")
