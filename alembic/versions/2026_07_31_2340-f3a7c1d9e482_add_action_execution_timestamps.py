"""add action execution timestamps

Revision ID: f3a7c1d9e482
Revises: 92ebe14532b9
Create Date: 2026-07-31T23:40:00+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a7c1d9e482"
down_revision: Union[str, None] = "92ebe14532b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("actions", sa.Column("started_at", sa.DateTime(), nullable=True))
    op.add_column("actions", sa.Column("finished_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("actions", "finished_at")
    op.drop_column("actions", "started_at")
