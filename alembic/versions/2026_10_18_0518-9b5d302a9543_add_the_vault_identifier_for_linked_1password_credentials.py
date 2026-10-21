"""Add the vault identifier for linked 1Password credentials.

Revision ID: 9b5d302a9543
Revises: 7e92edcb0f3b
Create Date: 2026-10-18T05:18:54.492326+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9b5d302a9543"
down_revision: Union[str, None] = "7e92edcb0f3b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("credentials", sa.Column("vault_id", sa.String(), nullable=True))
    op.create_index(
        "uq_credentials_org_vault_item",
        "credentials",
        ["organization_id", "vault_id", "item_id"],
        unique=True,
        postgresql_where=sa.text("vault_id IS NOT NULL AND deleted_at IS NULL"),
        sqlite_where=sa.text("vault_id IS NOT NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("uq_credentials_org_vault_item", table_name="credentials")
    op.drop_column("credentials", "vault_id")
