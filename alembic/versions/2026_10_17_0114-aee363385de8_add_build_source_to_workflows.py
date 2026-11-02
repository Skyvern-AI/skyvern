"""add build_source to workflows

Revision ID: aee363385de8
Revises: 0c453e5042cb
Create Date: 2026-10-17T01:14:43.134434+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aee363385de8"
down_revision: Union[str, None] = "0c453e5042cb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflows", sa.Column("build_source", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflows", "build_source")
