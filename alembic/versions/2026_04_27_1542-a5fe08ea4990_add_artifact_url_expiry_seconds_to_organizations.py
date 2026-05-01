"""add artifact_url_expiry_seconds to organizations

Revision ID: a5fe08ea4990
Revises: 2d0f9407ffac
Create Date: 2026-04-27T15:42:35.263273+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a5fe08ea4990"
down_revision: Union[str, None] = "2d0f9407ffac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("artifact_url_expiry_seconds", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "artifact_url_expiry_seconds")
