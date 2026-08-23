"""persist browser-session provisioning deadline

Revision ID: 49bb8a1289de
Revises: 3739fba3dc3d
Create Date: 2026-08-18T16:36:02.889812+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "49bb8a1289de"
down_revision: Union[str, None] = "3739fba3dc3d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.add_column("persistent_browser_sessions", sa.Column("provisioning_deadline_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.drop_column("persistent_browser_sessions", "provisioning_deadline_at")
