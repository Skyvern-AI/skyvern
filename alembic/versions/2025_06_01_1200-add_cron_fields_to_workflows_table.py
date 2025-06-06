"""Add cron fields to workflows table

Revision ID: addcron1234
Revises: babaa7307e8a
Create Date: 2025-06-01 12:00:00.000000+00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "addcron1234"
down_revision: Union[str, None] = "babaa7307e8a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workflows", sa.Column("cron_expression", sa.String(), nullable=True))
    op.add_column(
        "workflows",
        sa.Column("cron_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("workflows", "cron_enabled")
    op.drop_column("workflows", "cron_expression")
