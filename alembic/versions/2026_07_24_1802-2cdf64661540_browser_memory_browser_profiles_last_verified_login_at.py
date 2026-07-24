"""browser memory: browser_profiles.last_verified_login_at

Revision ID: 2cdf64661540
Revises: f244c178e9a9
Create Date: 2026-07-24T18:02:38.554154+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2cdf64661540"
down_revision: Union[str, None] = "f244c178e9a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("browser_profiles", sa.Column("last_verified_login_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("browser_profiles", "last_verified_login_at")
