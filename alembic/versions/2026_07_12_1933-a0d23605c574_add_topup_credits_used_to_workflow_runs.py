"""add_topup_credits_used_to_workflow_runs

Revision ID: a0d23605c574
Revises: 062b0fc39d36
Create Date: 2026-07-12T19:33:30.887512+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a0d23605c574"
down_revision: Union[str, None] = "062b0fc39d36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMN_NAME = "topup_credits_used"
_LOCK_TIMEOUT = "5s"


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    # Nullable ADD COLUMN with a constant server_default is metadata-only on PG 11+, safe on high-traffic workflow_runs.
    op.add_column("workflow_runs", sa.Column(_COLUMN_NAME, sa.Integer(), server_default="0", nullable=True))


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    op.drop_column("workflow_runs", _COLUMN_NAME)
