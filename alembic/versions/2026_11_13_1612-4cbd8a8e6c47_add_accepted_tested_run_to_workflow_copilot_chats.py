"""add accepted_tested_run to workflow copilot chats

Revision ID: 4cbd8a8e6c47
Revises: 5344ebc37000
Create Date: 2026-11-13T16:12:03.398632+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4cbd8a8e6c47"
down_revision: Union[str, None] = "5344ebc37000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_copilot_chats", sa.Column("accepted_tested_run", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_copilot_chats", "accepted_tested_run")
