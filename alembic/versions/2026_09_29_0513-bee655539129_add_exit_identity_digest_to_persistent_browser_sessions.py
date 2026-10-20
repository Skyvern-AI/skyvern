"""add exit_identity_digest to persistent_browser_sessions

Revision ID: bee655539129
Revises: 9fec57bdb481
Create Date: 2026-09-29T05:13:13.149896+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bee655539129"
down_revision: Union[str, None] = "9fec57bdb481"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("persistent_browser_sessions", sa.Column("exit_identity_digest", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("persistent_browser_sessions", "exit_identity_digest")
