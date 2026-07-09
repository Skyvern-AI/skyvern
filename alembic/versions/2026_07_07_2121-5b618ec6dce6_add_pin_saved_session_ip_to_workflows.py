"""add pin_saved_session_ip to workflows

Revision ID: 5b618ec6dce6
Revises: e2d87251fee8
Create Date: 2026-07-07T21:21:37.964227+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b618ec6dce6"
down_revision: Union[str, None] = "e2d87251fee8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "workflows",
        sa.Column("pin_saved_session_ip", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("workflows", "pin_saved_session_ip")
