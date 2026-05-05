"""add google_oauth_credentials (collapsed: consent + credential in one row)

Revision ID: 768c42c4968a
Revises: b19685399f97
Create Date: 2026-05-05T04:45:56.787945+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "768c42c4968a"
down_revision: Union[str, None] = "b19685399f97"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "google_oauth_credentials",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("credential_name", sa.String(), nullable=False, server_default="Default"),
        sa.Column("provider", sa.String(), nullable=False, server_default="google"),
        sa.Column("state", sa.String(), nullable=False, server_default="pending_consent"),
        sa.Column(
            "scopes_requested",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "scopes_granted",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("encrypted_refresh_token", sa.String(), nullable=True),
        sa.Column("encrypted_method", sa.String(), nullable=True),
        sa.Column("consent_nonce", sa.String(), nullable=True),
        sa.Column("consent_redirect_uri", sa.String(), nullable=True),
        sa.Column("consent_code_verifier", sa.String(), nullable=True),
        sa.Column("consent_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("modified_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.organization_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "state IN ('pending_consent', 'active', 'revoked', 'error')",
            name="ck_google_oauth_credentials_state",
        ),
    )
    op.create_index("ix_google_oauth_credentials_organization_id", "google_oauth_credentials", ["organization_id"])
    op.create_index(
        "ix_google_oauth_credentials_state",
        "google_oauth_credentials",
        ["state"],
    )
    # Partial unique index: a consent_nonce is only unique while it's in flight (pending_consent).
    # Post-promotion we null it out, so active/revoked/error rows never collide here.
    op.create_index(
        "ux_google_oauth_credentials_consent_nonce",
        "google_oauth_credentials",
        ["consent_nonce"],
        unique=True,
        postgresql_where=sa.text("consent_nonce IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_google_oauth_credentials_consent_nonce", table_name="google_oauth_credentials")
    op.drop_index("ix_google_oauth_credentials_state", table_name="google_oauth_credentials")
    op.drop_index("ix_google_oauth_credentials_organization_id", table_name="google_oauth_credentials")
    op.drop_table("google_oauth_credentials")
