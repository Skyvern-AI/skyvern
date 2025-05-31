"""Add cron fields to workflows table

Revision ID: 7d16d496abc1
Revises: af49ca791fc7
Create Date: 2025-06-01 12:00:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7d16d496abc1"
down_revision: Union[str, None] = "af49ca791fc7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workflows", sa.Column("cron_schedule", sa.String(), nullable=True))
    op.add_column("workflows", sa.Column("cron_timezone", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflows", "cron_timezone")
    op.drop_column("workflows", "cron_schedule")
