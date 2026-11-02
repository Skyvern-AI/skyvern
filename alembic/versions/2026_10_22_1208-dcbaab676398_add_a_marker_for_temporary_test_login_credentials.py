"""add a marker for temporary test-login credentials

Revision ID: dcbaab676398
Revises: 5112f44a9e61
Create Date: 2026-10-22T12:08:44.262351+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dcbaab676398"
down_revision: Union[str, None] = "5112f44a9e61"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("credentials", sa.Column("is_test_login_temporary", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("credentials", "is_test_login_temporary")
