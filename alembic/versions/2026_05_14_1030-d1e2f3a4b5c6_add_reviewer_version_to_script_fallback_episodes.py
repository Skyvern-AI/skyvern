"""add reviewer_version to script_fallback_episodes

Revision ID: d1e2f3a4b5c6
Revises: 0b0bd1875c6e
Create Date: 2026-05-14 10:30:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "0b0bd1875c6e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable string column. NULL = legacy (pre-tracking) episodes.
    # Populated going forward by the reviewer that processes the episode
    # ("v2", "v3"). Used to split metrics cleanly between cohorts.
    conn = op.get_bind()

    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'script_fallback_episodes' "
            "AND column_name = 'reviewer_version' "
            "AND table_schema = current_schema()"
        )
    )
    if not result.fetchone():
        op.add_column(
            "script_fallback_episodes",
            sa.Column("reviewer_version", sa.String(), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()

    column_exists = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'script_fallback_episodes' "
            "AND column_name = 'reviewer_version' "
            "AND table_schema = current_schema()"
        )
    ).fetchone()
    if column_exists:
        op.drop_column("script_fallback_episodes", "reviewer_version")
