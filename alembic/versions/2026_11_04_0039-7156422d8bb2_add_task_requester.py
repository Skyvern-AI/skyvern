"""add task requester

Revision ID: 7156422d8bb2
Revises: 711920698281
Create Date: 2026-11-04T00:39:38.871286+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7156422d8bb2"
down_revision: Union[str, None] = "711920698281"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("tasks", sa.Column("requested_by", sa.String(), nullable=True))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("tasks", "requested_by")
