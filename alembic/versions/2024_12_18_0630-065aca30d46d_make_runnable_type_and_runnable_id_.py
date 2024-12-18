"""make runnable_type and runnable_id nullable

Revision ID: 065aca30d46d
Revises: 282b0548d443
Create Date: 2024-12-18 06:30:46.558730+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '065aca30d46d'
down_revision: Union[str, None] = '282b0548d443'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column('persistent_browser_sessions', 'runnable_type',
               existing_type=sa.VARCHAR(),
               nullable=True)
    op.alter_column('persistent_browser_sessions', 'runnable_id',
               existing_type=sa.VARCHAR(),
               nullable=True)
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column('persistent_browser_sessions', 'runnable_id',
               existing_type=sa.VARCHAR(),
               nullable=False)
    op.alter_column('persistent_browser_sessions', 'runnable_type',
               existing_type=sa.VARCHAR(),
               nullable=False)
    # ### end Alembic commands ###