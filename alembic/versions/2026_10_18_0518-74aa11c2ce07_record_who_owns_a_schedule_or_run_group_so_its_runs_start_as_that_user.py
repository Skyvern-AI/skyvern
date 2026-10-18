"""Record who owns a schedule or run group so its runs start as that user.

Revision ID: 74aa11c2ce07
Revises: 1cf1687463a4
Create Date: 2026-10-18T05:18:54.482758+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "74aa11c2ce07"
down_revision: Union[str, None] = "1cf1687463a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_schedules", sa.Column("created_by", sa.String(), nullable=True))
    op.add_column("workflow_run_groups", sa.Column("created_by", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_run_groups", "created_by")
    op.drop_column("workflow_schedules", "created_by")
