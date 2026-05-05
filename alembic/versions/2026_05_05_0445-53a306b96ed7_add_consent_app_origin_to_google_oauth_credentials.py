"""add_consent_app_origin_to_google_oauth_credentials

Revision ID: 53a306b96ed7
Revises: 768c42c4968a
Create Date: 2026-05-05T04:45:56.788589+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "53a306b96ed7"
down_revision: Union[str, None] = "768c42c4968a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("google_oauth_credentials", sa.Column("consent_app_origin", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("google_oauth_credentials", "consent_app_origin")
