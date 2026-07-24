"""credential pin_saved_session_ip (keep the same IP for sign-ins with this credential)

Revision ID: b192c2ddb005
Revises: 2cdf64661540
Create Date: 2026-07-24T18:02:38.554328+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b192c2ddb005"
down_revision: Union[str, None] = "2cdf64661540"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "credentials",
        sa.Column("pin_saved_session_ip", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("credentials", "pin_saved_session_ip")
