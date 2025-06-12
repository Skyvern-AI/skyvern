"""add queued_at started_at finished_at columns

Revision ID: add_run_timestamps
Revises: babaa7307e8a
Create Date: 2025-06-02 00:50:00+00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "add_run_timestamps"
down_revision: Union[str, None] = "babaa7307e8a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("queued_at", sa.DateTime(), nullable=True))
    op.add_column("tasks", sa.Column("started_at", sa.DateTime(), nullable=True))
    op.add_column("tasks", sa.Column("finished_at", sa.DateTime(), nullable=True))

    op.add_column("observer_cruises", sa.Column("queued_at", sa.DateTime(), nullable=True))
    op.add_column("observer_cruises", sa.Column("started_at", sa.DateTime(), nullable=True))
    op.add_column("observer_cruises", sa.Column("finished_at", sa.DateTime(), nullable=True))

    op.add_column("workflow_runs", sa.Column("queued_at", sa.DateTime(), nullable=True))
    op.add_column("workflow_runs", sa.Column("started_at", sa.DateTime(), nullable=True))
    op.add_column("workflow_runs", sa.Column("finished_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_runs", "finished_at")
    op.drop_column("workflow_runs", "started_at")
    op.drop_column("workflow_runs", "queued_at")

    op.drop_column("observer_cruises", "finished_at")
    op.drop_column("observer_cruises", "started_at")
    op.drop_column("observer_cruises", "queued_at")

    op.drop_column("tasks", "finished_at")
    op.drop_column("tasks", "started_at")
    op.drop_column("tasks", "queued_at")
