"""merge multiple heads

Revision ID: 06a629c9f6ce
Revises: add_run_timestamps, 7bc030a082fa
Create Date: 2025-06-12 07:57:27.197504+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '06a629c9f6ce'
down_revision: Union[str, None] = ('add_run_timestamps', '7bc030a082fa')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
