"""Add credentials column to tasks table

Revision ID: add_task_credentials
Revises: 2025_06_19_2259-afeed80576cb_add_index_for_artifacts_table_
Create Date: 2025-01-24 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'add_task_credentials'
down_revision = '2025_06_19_2259-afeed80576cb_add_index_for_artifacts_table_'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add credentials column to tasks table
    op.add_column('tasks', sa.Column('credentials', sa.JSON(), nullable=True))


def downgrade() -> None:
    # Remove credentials column from tasks table
    op.drop_column('tasks', 'credentials') 