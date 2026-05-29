"""add narrative_payload column to workflow_copilot_chat_messages

Revision ID: ed48b2c9cab2
Revises: c636e511753d
Create Date: 2026-05-29T21:54:45.368634+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ed48b2c9cab2"
down_revision: Union[str, None] = "c636e511753d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    columns = {col["name"] for col in inspector.get_columns("workflow_copilot_chat_messages")}
    if "narrative_payload" in columns:
        return

    if conn.dialect.name == "postgresql":
        op.execute(sa.text("SET lock_timeout = '2s'"))

    op.add_column(
        "workflow_copilot_chat_messages",
        sa.Column("narrative_payload", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {col["name"] for col in inspector.get_columns("workflow_copilot_chat_messages")}
    if "narrative_payload" not in columns:
        return
    op.drop_column("workflow_copilot_chat_messages", "narrative_payload")
