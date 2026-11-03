"""add billing_exempt_at_admission to workflow_runs

Revision ID: 1bc01bfed6bf
Revises: 2dda9d1255a6
Create Date: 2026-09-25T05:56:18.608260+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1bc01bfed6bf"
down_revision: Union[str, None] = "2dda9d1255a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_runs", sa.Column("billing_exempt_at_admission", sa.Boolean(), nullable=True))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_runs", "billing_exempt_at_admission")
