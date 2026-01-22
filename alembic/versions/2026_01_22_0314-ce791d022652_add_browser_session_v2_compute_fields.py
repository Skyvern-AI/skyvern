"""add browser session v2 compute fields

Revision ID: ce791d022652
Revises: a720c991f779
Create Date: 2026-01-22 03:14:43.766916+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ce791d022652"
down_revision: Union[str, None] = "a720c991f779"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("persistent_browser_sessions", sa.Column("instance_type", sa.String(), nullable=True))
    op.add_column("persistent_browser_sessions", sa.Column("vcpu_millicores", sa.Integer(), nullable=True))
    op.add_column("persistent_browser_sessions", sa.Column("memory_mb", sa.Integer(), nullable=True))
    op.add_column("persistent_browser_sessions", sa.Column("duration_ms", sa.BigInteger(), nullable=True))
    op.add_column("persistent_browser_sessions", sa.Column("compute_cost", sa.Numeric(), nullable=True))


def downgrade() -> None:
    op.drop_column("persistent_browser_sessions", "compute_cost")
    op.drop_column("persistent_browser_sessions", "duration_ms")
    op.drop_column("persistent_browser_sessions", "memory_mb")
    op.drop_column("persistent_browser_sessions", "vcpu_millicores")
    op.drop_column("persistent_browser_sessions", "instance_type")
