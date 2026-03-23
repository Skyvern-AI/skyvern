"""add save_browser_session_intent to credentials

Revision ID: 6e966003a58e
Revises: 786acdf95243
Create Date: 2026-03-23 19:04:17.856273+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6e966003a58e'
down_revision: Union[str, None] = '786acdf95243'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "credentials",
        sa.Column("save_browser_session_intent", sa.Boolean(), nullable=True, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("credentials", "save_browser_session_intent")
