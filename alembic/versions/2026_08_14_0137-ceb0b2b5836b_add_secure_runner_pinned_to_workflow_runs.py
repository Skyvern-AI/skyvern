"""add secure_runner_pinned to workflow_runs

Revision ID: ceb0b2b5836b
Revises: a3b94b945dac
Create Date: 2026-08-14T01:37:28.825016+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ceb0b2b5836b"
down_revision: Union[str, None] = "a3b94b945dac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_runs", sa.Column("secure_runner_pinned", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_runs", "secure_runner_pinned")
