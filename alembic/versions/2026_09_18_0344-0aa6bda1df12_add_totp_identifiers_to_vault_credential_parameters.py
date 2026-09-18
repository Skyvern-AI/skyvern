"""add TOTP identifiers to vault credential parameters

Revision ID: 0aa6bda1df12
Revises: f65c97b21be8
Create Date: 2026-09-18T03:44:30.102805+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0aa6bda1df12"
down_revision: Union[str, None] = "f65c97b21be8"
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
        "onepassword_credential_parameters",
        sa.Column("totp_identifier", sa.String(), nullable=True),
    )
    op.add_column(
        "bitwarden_login_credential_parameters",
        sa.Column("totp_identifier", sa.String(), nullable=True),
    )


def downgrade() -> None:
    _set_local_ddl_timeouts()
    op.drop_column("bitwarden_login_credential_parameters", "totp_identifier")
    op.drop_column("onepassword_credential_parameters", "totp_identifier")
