"""add proxy pin fields

Revision ID: a1cad008154a
Revises: 45128d67bc1a
Create Date: 2026-06-26T19:26:11.232122+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1cad008154a"
down_revision: Union[str, None] = "45128d67bc1a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("credentials", sa.Column("proxy_location", sa.String(), nullable=True))
    op.add_column("credentials", sa.Column("proxy_session_id", sa.String(), nullable=True))
    op.add_column("browser_profiles", sa.Column("proxy_location", sa.String(), nullable=True))
    op.add_column("browser_profiles", sa.Column("proxy_session_id", sa.String(), nullable=True))
    op.add_column("persistent_browser_sessions", sa.Column("proxy_session_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("persistent_browser_sessions", "proxy_session_id")
    op.drop_column("browser_profiles", "proxy_session_id")
    op.drop_column("browser_profiles", "proxy_location")
    op.drop_column("credentials", "proxy_session_id")
    op.drop_column("credentials", "proxy_location")
