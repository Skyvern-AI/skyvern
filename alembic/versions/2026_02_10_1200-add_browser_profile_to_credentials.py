"""add browser_profile_id to credentials

Revision ID: a1b2c3d4e5f6
Revises: 43217e31df12
Create Date: 2026-02-10 12:00:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "43217e31df12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("credentials", sa.Column("browser_profile_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("credentials", "browser_profile_id")
