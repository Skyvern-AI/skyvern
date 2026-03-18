"""add script pinning columns

Revision ID: 7dc73e5b2fe4
Revises: d38cca7e13b8
Create Date: 2026-03-15 12:00:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7dc73e5b2fe4"
down_revision: Union[str, None] = "d38cca7e13b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workflow_scripts", sa.Column("is_pinned", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("workflow_scripts", sa.Column("pinned_at", sa.DateTime(), nullable=True))
    op.add_column("workflow_scripts", sa.Column("pinned_by", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_scripts", "pinned_by")
    op.drop_column("workflow_scripts", "pinned_at")
    op.drop_column("workflow_scripts", "is_pinned")
