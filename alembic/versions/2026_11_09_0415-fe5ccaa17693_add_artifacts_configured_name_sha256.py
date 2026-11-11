"""add artifacts configured_name_sha256

Revision ID: fe5ccaa17693
Revises: c9f6fd6fe36e
Create Date: 2026-11-09T04:15:13.877468+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fe5ccaa17693"
down_revision: Union[str, None] = "c9f6fd6fe36e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("artifacts", sa.Column("configured_name_sha256", sa.String(length=64), nullable=True))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("artifacts", "configured_name_sha256")
