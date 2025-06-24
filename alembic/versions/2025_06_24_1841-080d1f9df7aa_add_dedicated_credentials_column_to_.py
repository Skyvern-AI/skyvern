"""add_dedicated_credentials_column_to_tasks_and_task_v2

Revision ID: 080d1f9df7aa
Revises: b73ee94cf30e
Create Date: 2025-06-24 18:41:35.559526+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '080d1f9df7aa'
down_revision: Union[str, None] = 'b73ee94cf30e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add dedicated credentials column to tasks table
    op.add_column('tasks', sa.Column('credentials', sa.JSON(), nullable=True))
    
    # Add dedicated credentials column to task_v2 table (observer_cruises)
    op.add_column('observer_cruises', sa.Column('credentials', sa.JSON(), nullable=True))
    
    # Remove the incorrect parameters column from tasks table
    # (This was mistakenly used to store credentials)
    op.drop_column('tasks', 'parameters')


def downgrade() -> None:
    # Restore the parameters column
    op.add_column('tasks', sa.Column('parameters', sa.JSON(), nullable=True))
    
    # Remove the credentials columns
    op.drop_column('observer_cruises', 'credentials')
    op.drop_column('tasks', 'credentials')
