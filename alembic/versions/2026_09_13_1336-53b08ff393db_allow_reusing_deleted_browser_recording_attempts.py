"""allow reusing deleted browser recording attempts

Revision ID: 53b08ff393db
Revises: 4c2609d95265
Create Date: 2026-09-13T13:36:32.280811+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "53b08ff393db"
down_revision: Union[str, None] = "4c2609d95265"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uc_browser_recordings_org_attempt", "browser_recordings", type_="unique")
    op.create_index(
        "uq_browser_recordings_org_attempt_active",
        "browser_recordings",
        ["organization_id", "recording_attempt_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_browser_recordings_org_attempt_active", table_name="browser_recordings")
    op.create_unique_constraint(
        "uc_browser_recordings_org_attempt",
        "browser_recordings",
        ["organization_id", "recording_attempt_id"],
    )
