"""add turn_outcome column to workflow_copilot_chat_messages

Revision ID: 8a7754a48701
Revises: 729b4078a2e9
Create Date: 2026-05-24T18:12:12.662694+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8a7754a48701"
down_revision: Union[str, None] = "729b4078a2e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    columns = {col["name"] for col in inspector.get_columns("workflow_copilot_chat_messages")}
    if "turn_outcome" in columns:
        return

    if conn.dialect.name == "postgresql":
        op.execute(sa.text("SET lock_timeout = '2s'"))

    op.add_column(
        "workflow_copilot_chat_messages",
        sa.Column("turn_outcome", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {col["name"] for col in inspector.get_columns("workflow_copilot_chat_messages")}
    if "turn_outcome" not in columns:
        return
    op.drop_column("workflow_copilot_chat_messages", "turn_outcome")
