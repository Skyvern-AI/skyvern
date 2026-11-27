"""Add a durable filename for a validated session download.

Revision ID: 0acbbbf657e8
Revises: 9bed567e7833
Create Date: 2026-11-27T05:59:55.031775+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0acbbbf657e8"
down_revision: Union[str, None] = "9bed567e7833"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("artifacts", sa.Column("download_filename", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("artifacts", "download_filename")
