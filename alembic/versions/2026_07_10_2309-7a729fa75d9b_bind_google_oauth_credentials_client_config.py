"""bind google oauth credentials client config

Revision ID: 7a729fa75d9b
Revises: e1227914ecfe
Create Date: 2026-07-10T23:09:58.950836+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7a729fa75d9b"
down_revision: Union[str, None] = "e1227914ecfe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("google_oauth_credentials", sa.Column("client_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("google_oauth_credentials", "client_id")
