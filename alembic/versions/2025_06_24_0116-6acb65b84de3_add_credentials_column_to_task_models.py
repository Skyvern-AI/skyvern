"""Add credentials column to task models

Revision ID: 6acb65b84de3
Revises: afeed80576cb
Create Date: 2025-06-24 01:16:02.546301+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6acb65b84de3"
down_revision: Union[str, None] = "afeed80576cb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add credentials column to tasks table (TaskModel)
    op.add_column("tasks", sa.Column("credentials", sa.JSON(), nullable=True))

    # Add credentials column to observer_cruises table (TaskV2Model)
    op.add_column("observer_cruises", sa.Column("credentials", sa.JSON(), nullable=True))


def downgrade() -> None:
    # Remove credentials column from tasks table
    op.drop_column("tasks", "credentials")

    # Remove credentials column from observer_cruises table
    op.drop_column("observer_cruises", "credentials")
