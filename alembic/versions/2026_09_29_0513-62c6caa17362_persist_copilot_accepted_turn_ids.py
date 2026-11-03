"""persist copilot accepted turn ids

Revision ID: 62c6caa17362
Revises: bee655539129
Create Date: 2026-09-29T05:13:13.151630+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "62c6caa17362"
down_revision: Union[str, None] = "bee655539129"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_copilot_chats", sa.Column("accepted_turn_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_copilot_chats", "accepted_turn_ids")
