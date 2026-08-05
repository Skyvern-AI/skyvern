"""add runnable generation id to persistent browser sessions

Revision ID: 1ee67c216fca
Revises: e1ffa32660cc
Create Date: 2026-08-04T06:37:02.828507+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1ee67c216fca"
down_revision: Union[str, None] = "e1ffa32660cc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "persistent_browser_sessions",
        sa.Column("runnable_generation_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("persistent_browser_sessions", "runnable_generation_id")
