"""add audio_artifact_id to workflow_copilot_chat_messages

Revision ID: a8c4e2f9d6b1
Revises: f3a9c2b7e1d4
Create Date: 2026-06-11T20:30:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8c4e2f9d6b1"
down_revision: Union[str, None] = "f3a9c2b7e1d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    columns = {col["name"] for col in inspector.get_columns("workflow_copilot_chat_messages")}
    if "audio_artifact_id" in columns:
        return

    if conn.dialect.name == "postgresql":
        op.execute(sa.text("SET lock_timeout = '2s'"))

    op.add_column(
        "workflow_copilot_chat_messages",
        sa.Column("audio_artifact_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {col["name"] for col in inspector.get_columns("workflow_copilot_chat_messages")}
    if "audio_artifact_id" not in columns:
        return
    op.drop_column("workflow_copilot_chat_messages", "audio_artifact_id")
