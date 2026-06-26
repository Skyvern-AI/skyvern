"""add copilot chat history indexes

Revision ID: 61c9acf9f3ba
Revises: 58b0ced36529
Create Date: 2026-06-22 11:22:49.851792+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "61c9acf9f3ba"
down_revision: Union[str, None] = "58b0ced36529"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '1h';")
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS wccm_org_chat_index
            ON workflow_copilot_chat_messages (organization_id, workflow_copilot_chat_id);
        """)
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS wcc_org_created_at_index
            ON workflow_copilot_chats (organization_id, created_at);
        """)
        op.execute("RESET statement_timeout;")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS wccm_org_chat_index;")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS wcc_org_created_at_index;")
