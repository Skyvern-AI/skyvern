"""add workflow run setup completion marker

Revision ID: 0c453e5042cb
Revises: 50c59227239d
Create Date: 2026-10-16T22:21:24.323746+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0c453e5042cb"
down_revision: Union[str, None] = "50c59227239d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "workflow_runs",
        sa.Column("setup_completed", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_runs", "setup_completed")
