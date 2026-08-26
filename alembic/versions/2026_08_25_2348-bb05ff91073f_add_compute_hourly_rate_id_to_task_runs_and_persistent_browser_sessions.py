"""add compute_hourly_rate_id to task_runs and persistent_browser_sessions

Revision ID: bb05ff91073f
Revises: fd23c7ac01d0
Create Date: 2026-08-25T23:48:03.103453+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bb05ff91073f"
down_revision: Union[str, None] = "fd23c7ac01d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("task_runs", sa.Column("compute_hourly_rate_id", sa.BigInteger(), nullable=True))
    op.add_column("persistent_browser_sessions", sa.Column("compute_hourly_rate_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("persistent_browser_sessions", "compute_hourly_rate_id")
    op.drop_column("task_runs", "compute_hourly_rate_id")
