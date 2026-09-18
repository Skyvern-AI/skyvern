"""add non-secret TOTP seed presence to credentials

Revision ID: 2dace0960323
Revises: 0aa6bda1df12
Create Date: 2026-09-18T03:44:30.103718+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2dace0960323"
down_revision: Union[str, None] = "0aa6bda1df12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def _set_local_ddl_timeouts() -> None:
    """Bound live-table DDL waits, including after OSS code generation."""
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.execute("SET LOCAL statement_timeout = '3h'")


def upgrade() -> None:
    _set_local_ddl_timeouts()
    op.add_column(
        "credentials",
        sa.Column("has_totp_seed", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    _set_local_ddl_timeouts()
    op.drop_column("credentials", "has_totp_seed")
