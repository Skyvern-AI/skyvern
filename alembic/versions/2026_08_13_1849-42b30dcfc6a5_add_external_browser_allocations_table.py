"""add external_browser_allocations table

Revision ID: 42b30dcfc6a5
Revises: a3b94b945dac
Create Date: 2026-08-13T18:49:01.344878+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "42b30dcfc6a5"
down_revision: Union[str, None] = "a3b94b945dac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "external_browser_allocations",
        sa.Column("allocation_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=True),
        sa.Column("allocation_provider", sa.String(), nullable=False),
        sa.Column("provider_key", sa.String(), nullable=True),
        sa.Column("vendor_session_id", sa.String(), nullable=True),
        sa.Column("owner_kind", sa.String(), nullable=True),
        sa.Column("owner_id", sa.String(), nullable=True),
        sa.Column("recovery_policy", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("allocation_id"),
    )
    op.create_index(
        "ix_external_browser_allocations_open",
        "external_browser_allocations",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_index(
        "ix_external_browser_allocations_owner",
        "external_browser_allocations",
        ["owner_kind", "owner_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_external_browser_allocations_owner", table_name="external_browser_allocations")
    op.drop_index(
        "ix_external_browser_allocations_open",
        table_name="external_browser_allocations",
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.drop_table("external_browser_allocations")
