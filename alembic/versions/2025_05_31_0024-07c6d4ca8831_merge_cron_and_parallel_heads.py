"""merge cron and parallel heads

Revision ID: 07c6d4ca8831
Revises: d90729821ec3, 8b0d2b9acfea
Create Date: 2025-05-31 00:24:29.187231+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '07c6d4ca8831'
down_revision: Union[str, None] = ('d90729821ec3', '8b0d2b9acfea')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
