"""mark_onepassword_credential_parameters_as_existing

Revision ID: ad22aca7c6af
Revises: d9ce23f5729c
Create Date: 2025-06-10 03:48:07.456369+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "ad22aca7c6af"
down_revision: Union[str, None] = "d9ce23f5729c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # This is a no-op migration to handle the case where the table already exists
    # The table was created outside of the migration system, so we just need to mark it as existing
    pass


def downgrade() -> None:
    # This is a no-op since we don't want to drop the table on downgrade
    pass
