"""add downloaded_file_count to workflow_run_blocks

Revision ID: 58b0ced36529
Revises: 1fee32b3d7c6
Create Date: 2026-06-19T20:44:46.548990+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "58b0ced36529"
down_revision: Union[str, None] = "1fee32b3d7c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_run_blocks", sa.Column("downloaded_file_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_run_blocks", "downloaded_file_count")
