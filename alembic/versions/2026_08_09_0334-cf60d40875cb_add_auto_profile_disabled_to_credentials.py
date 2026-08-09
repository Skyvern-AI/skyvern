"""add auto profile disabled to credentials

Revision ID: cf60d40875cb
Revises: a3b94b945dac
Create Date: 2026-08-09T03:34:00.468240+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cf60d40875cb"
down_revision: Union[str, None] = "a3b94b945dac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("credentials", sa.Column("auto_profile_disabled", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("credentials", "auto_profile_disabled")
