"""Add per-run scrape screenshot limits.

Revision ID: 65bc4a7809f1
Revises: 2868bd257141
Create Date: 2026-11-02 12:00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "65bc4a7809f1"
down_revision = "2868bd257141"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Persist optional screenshot caps without changing existing runs."""
    for table in ("tasks", "observer_cruises", "workflow_runs"):
        op.add_column(table, sa.Column("max_scrape_screenshots", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove the per-run screenshot caps."""
    for table in ("workflow_runs", "observer_cruises", "tasks"):
        op.drop_column(table, "max_scrape_screenshots")
