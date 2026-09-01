"""add finish_reason to workflow_run_blocks

Revision ID: e29e30deaac9
Revises: 8a7f32c5f807
Create Date: 2026-08-30T21:15:10.745919+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e29e30deaac9"
down_revision: Union[str, None] = "8a7f32c5f807"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_run_blocks", sa.Column("finish_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_run_blocks", "finish_reason")
