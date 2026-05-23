"""add reviewer_version to script_fallback_episodes

Revision ID: 7389b537b3a4
Revises: 0b0bd1875c6e
Create Date: 2026-05-23T00:34:07.902717+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7389b537b3a4"
down_revision: Union[str, None] = "0b0bd1875c6e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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
