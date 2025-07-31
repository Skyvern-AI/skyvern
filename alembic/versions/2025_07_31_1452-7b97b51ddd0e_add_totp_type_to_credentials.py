"""add_totp_type_to_credentials

Revision ID: 7b97b51ddd0e
Revises: 0ecb03206fc6
Create Date: 2025-07-31 14:52:46.804631+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b97b51ddd0e"
down_revision: Union[str, None] = "0ecb03206fc6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add totp_type column to credentials table
    op.add_column("credentials", sa.Column("totp_type", sa.String(), nullable=True))
    # Set default value for existing records
    op.execute("UPDATE credentials SET totp_type = 'none' WHERE totp_type IS NULL")
    # Make column non-nullable after setting defaults
    op.alter_column("credentials", "totp_type", nullable=False, server_default="none")


def downgrade() -> None:
    # Remove totp_type column from credentials table
    op.drop_column("credentials", "totp_type")
