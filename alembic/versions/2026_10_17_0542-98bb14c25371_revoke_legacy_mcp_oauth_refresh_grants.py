"""revoke legacy MCP OAuth refresh grants

Revision ID: 98bb14c25371
Revises: aee363385de8
Create Date: 2026-10-17T05:42:29.589392+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "98bb14c25371"
down_revision: Union[str, None] = "aee363385de8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DELETE FROM mcp_oauth_refresh_tokens WHERE api_key_token_id IS NULL")


def downgrade() -> None:
    pass
