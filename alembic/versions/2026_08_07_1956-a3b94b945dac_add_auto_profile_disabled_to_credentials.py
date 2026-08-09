"""add auto profile disabled to credentials

Revision ID: a3b94b945dac
Revises: ec34d7f81ad4
Create Date: 2026-08-07 19:56:40-07:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3b94b945dac"
down_revision: Union[str, None] = "ec34d7f81ad4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("credentials", sa.Column("auto_profile_disabled", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("credentials", "auto_profile_disabled")
