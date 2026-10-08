"""add workflow run webhook delivery projection

Revision ID: 22abc3c996b2
Revises: 317a80337f2e
Create Date: 2026-09-22T21:21:07.515465+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "22abc3c996b2"
down_revision: Union[str, None] = "317a80337f2e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("webhook_delivery_status", sa.String(), nullable=True))
    op.add_column("workflow_runs", sa.Column("webhook_delivery_finalized_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_runs", "webhook_delivery_finalized_at")
    op.drop_column("workflow_runs", "webhook_delivery_status")
