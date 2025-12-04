"""add_import_tracking_to_workflows

Revision ID: b61cf349aa4b
Revises: 541870962332
Create Date: 2025-10-30 14:25:37.010446+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b61cf349aa4b"
down_revision: Union[str, None] = "541870962332"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add import_error column to workflows table for tracking import failures
    # Note: status column is a String, not an enum, so no schema changes needed for new status values
    op.add_column("workflows", sa.Column("import_error", sa.String(), nullable=True))


def downgrade() -> None:
    # Remove import_error column from workflows table
    op.drop_column("workflows", "import_error")
