"""add vendor live view metadata

Revision ID: 7b377e6a0388
Revises: e1ffa32660cc
Create Date: 2026-08-04T03:54:37.355375+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b377e6a0388"
down_revision: Union[str, None] = "e1ffa32660cc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("browser_session_infra", sa.Column("live_view_url", sa.Text(), nullable=True))
    op.add_column("browser_session_infra", sa.Column("live_view_protocol", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("browser_session_infra", "live_view_protocol")
    op.drop_column("browser_session_infra", "live_view_url")
