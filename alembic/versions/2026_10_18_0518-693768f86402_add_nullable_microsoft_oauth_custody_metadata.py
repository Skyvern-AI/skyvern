"""add nullable Microsoft OAuth custody metadata

Revision ID: 693768f86402
Revises: 9b5d302a9543
Create Date: 2026-10-18T05:18:54.493619+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "693768f86402"
down_revision: Union[str, None] = "9b5d302a9543"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for column in ("tenant_id", "object_id", "initiating_user_id", "connection_id", "custody_mode"):
        op.add_column("microsoft_oauth_credentials", sa.Column(column, sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for column in ("custody_mode", "connection_id", "initiating_user_id", "object_id", "tenant_id"):
        op.drop_column("microsoft_oauth_credentials", column)
