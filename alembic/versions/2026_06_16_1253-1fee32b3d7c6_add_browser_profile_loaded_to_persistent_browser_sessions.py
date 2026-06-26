"""add browser_profile_loaded to persistent_browser_sessions

Revision ID: 1fee32b3d7c6
Revises: cfc2318acdf9
Create Date: 2026-06-16T12:53:28.144752+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1fee32b3d7c6"
down_revision: Union[str, None] = "cfc2318acdf9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "persistent_browser_sessions",
        sa.Column("browser_profile_loaded", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("persistent_browser_sessions", "browser_profile_loaded")
