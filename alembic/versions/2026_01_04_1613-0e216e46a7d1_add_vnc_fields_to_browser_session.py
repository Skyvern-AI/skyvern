"""add vnc fields to browser session

Revision ID: 0e216e46a7d1
Revises: e393f33ec711
Create Date: 2026-01-04 16:13:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0e216e46a7d1"
down_revision: Union[str, None] = "e393f33ec711"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("persistent_browser_sessions", sa.Column("display_number", sa.Integer(), nullable=True))
    op.add_column("persistent_browser_sessions", sa.Column("vnc_port", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("persistent_browser_sessions", "vnc_port")
    op.drop_column("persistent_browser_sessions", "display_number")
