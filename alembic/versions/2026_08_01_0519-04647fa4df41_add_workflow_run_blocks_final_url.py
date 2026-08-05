"""add workflow_run_blocks.final_url

Revision ID: 04647fa4df41
Revises: f3a7c1d9e482
Create Date: 2026-08-01T05:19:15.224002+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "04647fa4df41"
down_revision: Union[str, None] = "f3a7c1d9e482"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.add_column("workflow_run_blocks", sa.Column("final_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.drop_column("workflow_run_blocks", "final_url")
