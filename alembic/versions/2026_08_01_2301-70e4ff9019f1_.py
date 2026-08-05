"""

Revision ID: 70e4ff9019f1
Revises: 04647fa4df41
Create Date: 2026-08-01T23:01:51.557562+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "70e4ff9019f1"
down_revision: Union[str, None] = "04647fa4df41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workflows",
        sa.Column("mask_secrets", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("workflows", "mask_secrets")
