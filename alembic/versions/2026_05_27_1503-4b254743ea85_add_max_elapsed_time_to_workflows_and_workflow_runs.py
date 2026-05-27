"""add max elapsed time to workflows and workflow_runs

Revision ID: 4b254743ea85
Revises: 8a7754a48701
Create Date: 2026-05-27T15:03:10.396250+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4b254743ea85"
down_revision: Union[str, None] = "8a7754a48701"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    op.add_column("workflows", sa.Column(_COLUMN_NAME, sa.Integer(), nullable=True))
    # Nullable ADD COLUMN without a default is metadata-only on PG 11+, including on high-traffic workflow_runs.
    op.add_column("workflow_runs", sa.Column(_COLUMN_NAME, sa.Integer(), nullable=True))


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    op.drop_column("workflow_runs", _COLUMN_NAME)
    op.drop_column("workflows", _COLUMN_NAME)
