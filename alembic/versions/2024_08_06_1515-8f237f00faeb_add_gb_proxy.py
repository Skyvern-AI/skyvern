"""Add GB proxy

Revision ID: 8f237f00faeb
Revises: c5ed5a3a14eb
Create Date: 2024-08-06 15:15:15.369986+00:00

"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy.engine import Connection

# revision identifiers, used by Alembic.
revision: str = "8f237f00faeb"
down_revision: Union[str, None] = "c5ed5a3a14eb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn: Connection = op.get_bind()
    if conn.dialect.name != "sqlite":
        op.execute("ALTER TYPE proxylocation ADD VALUE 'RESIDENTIAL_GB'")
    # SQLite does not support ALTER TYPE


def downgrade() -> None:
    pass
