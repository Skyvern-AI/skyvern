"""add action execution timestamps

Revision ID: d240ff578c33
Revises: 92ebe14532b9
Create Date: 2026-07-31T22:51:44.512891+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d240ff578c33"
down_revision: Union[str, None] = "92ebe14532b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("actions", sa.Column("started_at", sa.DateTime(), nullable=True))
    op.add_column("actions", sa.Column("finished_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("actions", "finished_at")
    op.drop_column("actions", "started_at")
