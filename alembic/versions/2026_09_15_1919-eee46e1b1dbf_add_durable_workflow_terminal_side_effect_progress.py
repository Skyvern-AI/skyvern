"""Add durable workflow terminal side-effect progress.

Revision ID: eee46e1b1dbf
Revises: 2670d5a61131
Create Date: 2026-09-15T19:19:18.481815+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "eee46e1b1dbf"
down_revision: Union[str, None] = "2670d5a61131"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_run_attempts", sa.Column("interim_side_effects_progress", sa.JSON(), nullable=True))
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_run_attempts", sa.Column("final_side_effects_progress", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_run_attempts", "final_side_effects_progress")
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_run_attempts", "interim_side_effects_progress")
