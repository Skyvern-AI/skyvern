"""add created_by to workflow_runs

Revision ID: adf6f1f2e672
Revises: 762d72647b18
Create Date: 2026-09-27T02:34:31.800080+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "adf6f1f2e672"
down_revision: Union[str, None] = "762d72647b18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_runs", sa.Column("created_by", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_runs", "created_by")
