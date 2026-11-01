"""add created_at index for inbound email retention scans

Revision ID: 626683df7bd7
Revises: 7ec5ab2733c3
Create Date: 2026-11-01T14:18:08.990273+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "626683df7bd7"
down_revision: Union[str, None] = "7ec5ab2733c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_index(
        "ix_organization_inbound_emails_created_at",
        "organization_inbound_emails",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_organization_inbound_emails_created_at", table_name="organization_inbound_emails")
