"""add script_run to workflow_run_blocks

Revision ID: d1474f2d1581
Revises: c19d7d385560
Create Date: 2026-04-25T00:46:05.444225+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1474f2d1581"
down_revision: Union[str, None] = "c19d7d385560"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workflow_run_blocks",
        sa.Column("script_run", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workflow_run_blocks", "script_run")
