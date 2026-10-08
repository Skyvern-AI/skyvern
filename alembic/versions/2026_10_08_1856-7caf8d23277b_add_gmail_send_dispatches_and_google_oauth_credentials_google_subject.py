"""add gmail_send_dispatches and google_oauth_credentials.google_subject

Revision ID: 7caf8d23277b
Revises: 03bf12e00bee
Create Date: 2026-10-08T18:56:05.825411+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7caf8d23277b"
down_revision: Union[str, None] = "03bf12e00bee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("google_oauth_credentials", sa.Column("google_subject", sa.String(), nullable=True))
    op.create_table(
        "gmail_send_dispatches",
        sa.Column("gmail_send_dispatch_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("workflow_run_id", sa.String(), nullable=False),
        sa.Column("execution_key", sa.String(), nullable=False),
        sa.Column("block_label", sa.String(), nullable=False),
        sa.Column("credential_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("provider_message_id", sa.String(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("provider_status", sa.Integer(), nullable=True),
        sa.Column("provider_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('dispatching', 'accepted', 'failed', 'unknown')",
            name="ck_gmail_send_dispatches_status",
        ),
        sa.PrimaryKeyConstraint("gmail_send_dispatch_id"),
        sa.UniqueConstraint("workflow_run_id", "execution_key", name="uq_gmail_send_dispatches_execution"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("gmail_send_dispatches")
    op.drop_column("google_oauth_credentials", "google_subject")
