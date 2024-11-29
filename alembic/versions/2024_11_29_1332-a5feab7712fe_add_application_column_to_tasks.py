"""Add application column to tasks

Revision ID: a5feab7712fe
Revises: 56085e451bec
Create Date: 2024-11-29 13:32:58.845703+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a5feab7712fe"
down_revision: Union[str, None] = "56085e451bec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("application", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "application")
