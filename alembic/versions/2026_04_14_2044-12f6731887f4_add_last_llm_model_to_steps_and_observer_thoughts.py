"""add last_llm_model to steps and observer_thoughts

Revision ID: 12f6731887f4
Revises: f7bf5845eafb
Create Date: 2026-04-14T20:44:17.805248+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "12f6731887f4"
down_revision: Union[str, None] = "f7bf5845eafb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # Add last_llm_model to steps
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'steps' "
            "AND column_name = 'last_llm_model' "
            "AND table_schema = current_schema()"
        )
    )
    if not result.fetchone():
        op.add_column("steps", sa.Column("last_llm_model", sa.String(), nullable=True))

    # Add last_llm_model to observer_thoughts
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'observer_thoughts' "
            "AND column_name = 'last_llm_model' "
            "AND table_schema = current_schema()"
        )
    )
    if not result.fetchone():
        op.add_column("observer_thoughts", sa.Column("last_llm_model", sa.String(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()

    column_exists = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'observer_thoughts' "
            "AND column_name = 'last_llm_model' "
            "AND table_schema = current_schema()"
        )
    ).fetchone()
    if column_exists:
        op.drop_column("observer_thoughts", "last_llm_model")

    column_exists = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'steps' "
            "AND column_name = 'last_llm_model' "
            "AND table_schema = current_schema()"
        )
    ).fetchone()
    if column_exists:
        op.drop_column("steps", "last_llm_model")
