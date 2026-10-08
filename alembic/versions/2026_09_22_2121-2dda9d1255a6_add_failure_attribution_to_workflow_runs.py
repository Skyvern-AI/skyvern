"""add failure attribution to workflow runs

Revision ID: 2dda9d1255a6
Revises: 22abc3c996b2
Create Date: 2026-09-22T21:21:07.517241+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2dda9d1255a6"
down_revision: Union[str, None] = "22abc3c996b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_runs", sa.Column("failure_attribution", sa.JSON(none_as_null=True), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_runs", "failure_attribution")
