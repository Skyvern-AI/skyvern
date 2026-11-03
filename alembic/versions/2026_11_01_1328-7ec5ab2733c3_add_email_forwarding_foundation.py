"""add email forwarding foundation

Revision ID: 7ec5ab2733c3
Revises: bbc28e00784e
Create Date: 2026-11-01T13:28:28.564882+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7ec5ab2733c3"
down_revision: Union[str, None] = "bbc28e00784e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.create_table(
        "organization_email_configs",
        sa.Column("email_config_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("webhook_secret_hash", sa.String(), nullable=True),
        sa.Column("daily_inbound_cap", sa.Integer(), server_default="500", nullable=False),
        sa.Column("drop_flagged_spam", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.organization_id"]),
        sa.PrimaryKeyConstraint("email_config_id"),
    )
    op.create_index(
        "ix_organization_email_configs_org",
        "organization_email_configs",
        ["organization_id"],
        unique=False,
    )

    op.create_table(
        "organization_forwarding_addresses",
        sa.Column("forwarding_address_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("email_config_id", sa.String(), nullable=False),
        sa.Column("email_address", sa.String(), nullable=False),
        sa.Column("org_slug", sa.String(), nullable=False),
        sa.Column("match_mode", sa.String(), nullable=False),
        sa.Column("credential_id", sa.String(), nullable=True),
        sa.Column("sender_allowlist", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("last_received_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["credential_id"], ["credentials.credential_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["email_config_id"], ["organization_email_configs.email_config_id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.organization_id"]),
        sa.PrimaryKeyConstraint("forwarding_address_id"),
    )
    op.create_index(
        "ix_organization_forwarding_addresses_org",
        "organization_forwarding_addresses",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_organization_forwarding_addresses_config",
        "organization_forwarding_addresses",
        ["email_config_id"],
        unique=False,
    )
    op.create_index(
        "uq_organization_forwarding_addresses_email",
        "organization_forwarding_addresses",
        ["email_address"],
        unique=True,
        postgresql_where=sa.text("status != 'revoked' AND deleted_at IS NULL"),
    )

    op.create_table(
        "organization_inbound_emails",
        sa.Column("inbound_email_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("forwarding_address_id", sa.String(), nullable=False),
        sa.Column("external_message_id", sa.Text(), nullable=True),
        sa.Column("from_address", sa.Text(), nullable=False),
        sa.Column("original_recipient", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("links", sa.JSON(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("verdicts", sa.JSON(), nullable=True),
        sa.Column("totp_code_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["forwarding_address_id"], ["organization_forwarding_addresses.forwarding_address_id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.organization_id"]),
        sa.ForeignKeyConstraint(["totp_code_id"], ["totp_codes.totp_code_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("inbound_email_id"),
        sa.UniqueConstraint(
            "organization_id", "external_message_id", name="uq_organization_inbound_emails_external_message"
        ),
    )
    op.create_index(
        "ix_organization_inbound_emails_address_created",
        "organization_inbound_emails",
        ["forwarding_address_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_organization_inbound_emails_address_created", table_name="organization_inbound_emails")
    op.drop_table("organization_inbound_emails")
    op.drop_index("ix_organization_forwarding_addresses_config", table_name="organization_forwarding_addresses")
    op.drop_index("ix_organization_forwarding_addresses_org", table_name="organization_forwarding_addresses")
    op.drop_table("organization_forwarding_addresses")
    op.drop_index("ix_organization_email_configs_org", table_name="organization_email_configs")
    op.drop_table("organization_email_configs")
