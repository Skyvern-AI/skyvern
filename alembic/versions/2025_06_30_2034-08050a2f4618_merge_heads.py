"""merge heads

Revision ID: 08050a2f4618
Revises: 760ae45a1345, 6cf2c1e15039
Create Date: 2025-06-30 20:34:17.004988+00:00

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = '08050a2f4618'
down_revision: Union[str, Sequence[str], None] = ('760ae45a1345', '6cf2c1e15039')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
