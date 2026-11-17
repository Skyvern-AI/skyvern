"""Record the key that created schedules.

Revision ID: a917f1c21903
Revises: 4cbd8a8e6c47
Create Date: 2026-11-17T15:50:11.765306+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a917f1c21903"
down_revision: Union[str, None] = "4cbd8a8e6c47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_schedules", sa.Column("requested_by", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_schedules", "requested_by")
