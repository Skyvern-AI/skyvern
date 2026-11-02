"""Add execution leases for in-process runs.

Revision ID: 50c59227239d
Revises: 4452dbf24863
Create Date: 2026-10-16T07:38:57.828780+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "50c59227239d"
down_revision: Union[str, None] = "4452dbf24863"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "run_execution_leases",
        sa.Column("run_id", sa.String(), primary_key=True),
        sa.Column("run_type", sa.String(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("run_execution_leases")
