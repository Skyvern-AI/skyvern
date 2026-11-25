"""Record the user who completed consent on each connected account.

Revision ID: 9bed567e7833
Revises: 72057e9ec892
Create Date: 2026-11-25T15:35:27.927806+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9bed567e7833"
down_revision: Union[str, None] = "72057e9ec892"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("google_oauth_credentials", sa.Column("initiating_user_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("google_oauth_credentials", "initiating_user_id")
