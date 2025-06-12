"""db script for 1password integration

Revision ID: 1517a4ba63fa
Revises: 7bc030a082fa
Create Date: 2025-06-12 08:06:13.439802+00:00

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "1517a4ba63fa"
down_revision: Union[str, Sequence[str], None] = ("add_run_timestamps", "7bc030a082fa")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op: table created in previous revision."""


def downgrade() -> None:
    """No-op downgrade corresponding to the no-op upgrade."""
