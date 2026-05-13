"""add file size to artifacts

Revision ID: 78a8db531e69
Revises: 289f2d72b0c2
Create Date: 2026-05-08T08:15:20.233046+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "78a8db531e69"
down_revision: Union[str, None] = "289f2d72b0c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.add_column("artifacts", sa.Column("file_size", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.drop_column("artifacts", "file_size")
