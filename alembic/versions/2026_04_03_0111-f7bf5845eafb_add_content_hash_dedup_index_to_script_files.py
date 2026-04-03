"""add_content_hash_dedup_index_to_script_files

Revision ID: f7bf5845eafb
Revises: 5516c5bf7762
Create Date: 2026-04-03T01:11:07.051219+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7bf5845eafb"
down_revision: Union[str, None] = "5516c5bf7762"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_script_files_dedup", "script_files", ["script_id", "organization_id", "content_hash"])


def downgrade() -> None:
    op.drop_index("ix_script_files_dedup", table_name="script_files")
