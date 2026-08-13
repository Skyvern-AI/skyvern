"""add run owned reaper candidate state

Revision ID: 5a2119e4256c
Revises: 42b30dcfc6a5
Create Date: 2026-08-13T18:49:01.345254+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5a2119e4256c"
down_revision: Union[str, None] = "42b30dcfc6a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("external_browser_allocations", sa.Column("last_reap_attempt_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_external_browser_allocations_run_owned_reaper_candidates",
        "external_browser_allocations",
        ["recovery_policy", "created_at"],
        unique=False,
        postgresql_where=sa.text("closed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_external_browser_allocations_run_owned_reaper_candidates",
        table_name="external_browser_allocations",
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.drop_column("external_browser_allocations", "last_reap_attempt_at")
