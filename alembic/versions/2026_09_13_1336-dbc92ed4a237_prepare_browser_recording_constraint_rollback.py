"""prepare browser recording constraint rollback

Revision ID: dbc92ed4a237
Revises: 53b08ff393db
Create Date: 2026-09-13T13:36:32.281739+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dbc92ed4a237"
down_revision: Union[str, None] = "53b08ff393db"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM browser_recordings
        WHERE recording_id IN (
            SELECT recording_id
            FROM (
                SELECT
                    recording_id,
                    row_number() OVER (
                        PARTITION BY organization_id, recording_attempt_id
                        ORDER BY (deleted_at IS NULL) DESC, created_at DESC
                    ) AS duplicate_number
                FROM browser_recordings
            ) AS ranked_recordings
            WHERE duplicate_number > 1
        )
        """
    )
