"""add browser_profile_id to workflows

Revision ID: d8f04a23d52b
Revises: 5b15948c8d68
Create Date: 2026-05-12T20:26:33.860249+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8f04a23d52b"
down_revision: Union[str, None] = "5b15948c8d68"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workflows", sa.Column("browser_profile_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflows", "browser_profile_id")
