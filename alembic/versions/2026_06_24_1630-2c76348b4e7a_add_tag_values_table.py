"""add tag values table

Revision ID: 2c76348b4e7a
Revises: 61c9acf9f3ba
Create Date: 2026-06-24T16:30:09.366617+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2c76348b4e7a"
down_revision: Union[str, None] = "61c9acf9f3ba"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tag_values",
        sa.Column("tag_value_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("color", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
        ),
        sa.PrimaryKeyConstraint("tag_value_id"),
    )
    op.create_index(
        "ix_tag_values_org_key_value_active",
        "tag_values",
        ["organization_id", "key", "value"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tag_values_org_key_value_active",
        table_name="tag_values",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_table("tag_values")
