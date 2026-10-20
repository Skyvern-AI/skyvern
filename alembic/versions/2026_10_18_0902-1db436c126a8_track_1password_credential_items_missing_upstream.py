"""Track 1Password credential items missing upstream.

Revision ID: 1db436c126a8
Revises: 693768f86402
Create Date: 2026-10-18T09:02:51.669010+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1db436c126a8"
down_revision: Union[str, None] = "693768f86402"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("credentials", sa.Column("upstream_missing_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("credentials", "upstream_missing_at")
