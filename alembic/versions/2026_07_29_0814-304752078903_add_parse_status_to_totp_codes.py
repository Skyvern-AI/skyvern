"""add parse status to totp codes

Revision ID: 304752078903
Revises: b0941c6bd241
Create Date: 2026-07-29T08:14:26.430848+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "304752078903"
down_revision: Union[str, None] = "b0941c6bd241"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("totp_codes", "code", existing_type=sa.String(), nullable=True)
    op.add_column(
        "totp_codes",
        sa.Column("parse_status", sa.String(), nullable=False, server_default="parsed"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM totp_codes WHERE parse_status = 'raw' OR code IS NULL")
    op.drop_column("totp_codes", "parse_status")
    op.alter_column("totp_codes", "code", existing_type=sa.String(), nullable=False)
