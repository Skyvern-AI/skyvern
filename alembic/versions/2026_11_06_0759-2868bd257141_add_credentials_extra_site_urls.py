"""add credentials.extra_site_urls

Revision ID: 2868bd257141
Revises: 7156422d8bb2
Create Date: 2026-11-06T07:59:33.368702+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2868bd257141"
down_revision: Union[str, None] = "7156422d8bb2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("credentials", sa.Column("extra_site_urls", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("credentials", "extra_site_urls")
