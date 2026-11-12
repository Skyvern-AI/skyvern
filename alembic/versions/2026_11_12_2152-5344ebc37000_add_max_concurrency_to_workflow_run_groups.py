"""add max_concurrency to workflow run groups

Revision ID: 5344ebc37000
Revises: fe5ccaa17693
Create Date: 2026-11-12T21:52:39.527301+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5344ebc37000"
down_revision: Union[str, None] = "fe5ccaa17693"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "workflow_run_groups",
        sa.Column("max_concurrency", sa.Integer(), server_default="1", nullable=True),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_run_groups", "max_concurrency")
