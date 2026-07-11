"""add microsoft_oauth_credentials

Revision ID: 062b0fc39d36
Revises: 7a729fa75d9b
Create Date: 2026-07-11T02:15:25.519747+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "062b0fc39d36"
down_revision: Union[str, None] = "7a729fa75d9b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "microsoft_oauth_credentials",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("credential_name", sa.String(), nullable=False, server_default="Default"),
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
        sa.Column("consent_app_origin", sa.String(), nullable=True),
        sa.Column("consent_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("modified_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.organization_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "state IN ('pending_consent', 'active', 'revoked', 'error')",
            name="ck_microsoft_oauth_credentials_state",
        ),
    )
    op.create_index(
        "ix_microsoft_oauth_credentials_organization_id",
        "microsoft_oauth_credentials",
        ["organization_id"],
    )
    op.create_index(
        "ix_microsoft_oauth_credentials_state",
        "microsoft_oauth_credentials",
        ["state"],
    )
    op.create_index(
        "ux_microsoft_oauth_credentials_consent_nonce",
        "microsoft_oauth_credentials",
        ["consent_nonce"],
        unique=True,
        postgresql_where=sa.text("consent_nonce IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_microsoft_oauth_credentials_consent_nonce", table_name="microsoft_oauth_credentials")
    op.drop_index("ix_microsoft_oauth_credentials_state", table_name="microsoft_oauth_credentials")
    op.drop_index("ix_microsoft_oauth_credentials_organization_id", table_name="microsoft_oauth_credentials")
    op.drop_table("microsoft_oauth_credentials")
