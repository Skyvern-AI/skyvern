"""persist scoped key requester for workflow run groups

Revision ID: 1cdd5c69bd07
Revises: 77ff13cea42b
Create Date: 2026-11-02T15:38:04.829744+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1cdd5c69bd07"
down_revision: Union[str, None] = "77ff13cea42b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_run_groups", sa.Column("requested_by", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_run_groups", "requested_by")
