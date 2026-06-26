"""add browser_profile_key to workflows

Revision ID: 45128d67bc1a
Revises: 2c76348b4e7a
Create Date: 2026-06-25T00:17:17.827852+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "45128d67bc1a"
down_revision: Union[str, None] = "2c76348b4e7a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workflows", sa.Column("browser_profile_key", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflows", "browser_profile_key")
