"""add auto profile disabled to credentials

Revision ID: 26a50b612f3d
Revises: ec34d7f81ad4
Create Date: 2026-08-07T18:00:40.538198+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "26a50b612f3d"
down_revision: Union[str, None] = "ec34d7f81ad4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("credentials", sa.Column("auto_profile_disabled", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("credentials", "auto_profile_disabled")
