"""add http_request block columns to workflow_run_blocks

Revision ID: c3323aaa8b4c
Revises: babaa7307e8a
Create Date: 2025-06-05 12:00:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "c3323aaa8b4c"
down_revision: Union[str, None] = "babaa7307e8a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workflow_run_blocks", sa.Column("url", sa.String(), nullable=True))
    op.add_column("workflow_run_blocks", sa.Column("method", sa.String(), nullable=True))
    op.add_column("workflow_run_blocks", sa.Column("headers", sa.JSON(), nullable=True))
    op.add_column("workflow_run_blocks", sa.Column("body", sa.JSON(), nullable=True))
    op.add_column("workflow_run_blocks", sa.Column("timeout", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_run_blocks", "timeout")
    op.drop_column("workflow_run_blocks", "body")
    op.drop_column("workflow_run_blocks", "headers")
    op.drop_column("workflow_run_blocks", "method")
    op.drop_column("workflow_run_blocks", "url")
