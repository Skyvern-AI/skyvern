"""add screenshot_artifact_id to actions

Revision ID: 3545c24f02f6
Revises: db8667f8ce63
Create Date: 2026-01-06 22:54:15.401625+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3545c24f02f6"
down_revision: Union[str, None] = "db8667f8ce63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("actions", sa.Column("screenshot_artifact_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("actions", "screenshot_artifact_id")
