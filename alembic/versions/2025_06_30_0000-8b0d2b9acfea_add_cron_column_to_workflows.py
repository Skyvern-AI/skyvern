"""Add cron column to workflows table

Revision ID: 8b0d2b9acfea
Revises: af49ca791fc7
Create Date: 2025-06-30 00:00:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8b0d2b9acfea"
down_revision: Union[str, None] = "af49ca791fc7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workflows", sa.Column("cron", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflows", "cron")
