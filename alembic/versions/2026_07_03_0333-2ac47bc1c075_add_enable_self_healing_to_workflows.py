"""add enable_self_healing to workflows

Revision ID: 2ac47bc1c075
Revises: ecf365563e98
Create Date: 2026-07-03T03:33:20.863940+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2ac47bc1c075"
down_revision: Union[str, None] = "ecf365563e98"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workflows",
        sa.Column("enable_self_healing", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("workflows", "enable_self_healing")
