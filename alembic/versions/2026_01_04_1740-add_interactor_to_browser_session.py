"""add interactor to browser session

Revision ID: add_interactor_01
Revises: 0e216e46a7d1
Create Date: 2026-01-04 17:40:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_interactor_01"
down_revision: Union[str, None] = "0e216e46a7d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("persistent_browser_sessions", sa.Column("interactor", sa.String(), nullable=True, server_default="agent"))


def downgrade() -> None:
    op.drop_column("persistent_browser_sessions", "interactor")
