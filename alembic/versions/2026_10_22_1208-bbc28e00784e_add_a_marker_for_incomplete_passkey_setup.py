"""add a marker for incomplete passkey setup

Revision ID: bbc28e00784e
Revises: dcbaab676398
Create Date: 2026-10-22T12:08:44.264921+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bbc28e00784e"
down_revision: Union[str, None] = "dcbaab676398"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("credentials", sa.Column("is_passkey_setup_pending", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("credentials", "is_passkey_setup_pending")
