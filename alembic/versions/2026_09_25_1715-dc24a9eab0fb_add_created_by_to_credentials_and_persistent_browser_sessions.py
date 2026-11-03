"""add created_by to credentials and persistent_browser_sessions

Revision ID: dc24a9eab0fb
Revises: 4909d15f6587
Create Date: 2026-09-25T17:15:02.859825+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dc24a9eab0fb"
down_revision: Union[str, None] = "4909d15f6587"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("credentials", sa.Column("created_by", sa.String(), nullable=True))
    op.add_column("persistent_browser_sessions", sa.Column("created_by", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("persistent_browser_sessions", "created_by")
    op.drop_column("credentials", "created_by")
