"""merge migration heads

Revision ID: 2c34dee3304e
Revises: b61cf349aa4b, 7fbf463be9a7
Create Date: 2025-11-05 15:23:24.380086+00:00

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "2c34dee3304e"
down_revision: Union[str, None] = ("b61cf349aa4b", "7fbf463be9a7")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
