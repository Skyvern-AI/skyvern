"""add audio artifact id to workflow copilot chat messages

Revision ID: 411b641f1b89
Revises: f3a9c2b7e1d4
Create Date: 2026-06-10T20:42:51.445362+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "411b641f1b89"
down_revision: Union[str, None] = "f3a9c2b7e1d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workflow_copilot_chat_messages",
        sa.Column("audio_artifact_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workflow_copilot_chat_messages", "audio_artifact_id")
