"""add oauth credential email addresses

Revision ID: 9d85c51a8468
Revises: 64cbada89956
Create Date: 2026-07-31T15:22:47.853089+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d85c51a8468"
down_revision: Union[str, None] = "64cbada89956"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("google_oauth_credentials", sa.Column("email_address", sa.String(), nullable=True))
    op.add_column("microsoft_oauth_credentials", sa.Column("email_address", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("microsoft_oauth_credentials", "email_address")
    op.drop_column("google_oauth_credentials", "email_address")
