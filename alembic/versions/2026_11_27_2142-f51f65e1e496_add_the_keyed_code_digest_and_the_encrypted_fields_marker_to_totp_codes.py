"""Add the keyed code digest and the encrypted-fields marker to totp_codes.

Revision ID: f51f65e1e496
Revises: 0acbbbf657e8
Create Date: 2026-11-27T21:42:20.749035+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f51f65e1e496"
down_revision: Union[str, None] = "0acbbbf657e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("totp_codes", sa.Column("code_digest", sa.String(), nullable=True))
    op.add_column("totp_codes", sa.Column("fields_encrypted", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("totp_codes", "fields_encrypted")
    op.drop_column("totp_codes", "code_digest")
