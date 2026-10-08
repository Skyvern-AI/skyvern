"""index run group items by item key

Revision ID: 8f3cbbcbbf34
Revises: 798a79449b1e
Create Date: 2026-10-05T04:12:47.344306+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f3cbbcbbf34"
down_revision: Union[str, None] = "798a79449b1e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_workflow_run_group_items_item_key_created_at",
        "workflow_run_group_items",
        ["item_key", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_run_group_items_item_key_created_at", table_name="workflow_run_group_items")
