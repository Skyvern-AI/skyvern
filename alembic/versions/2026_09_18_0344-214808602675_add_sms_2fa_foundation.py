"""add SMS 2FA foundation

Revision ID: 214808602675
Revises: eee46e1b1dbf
Create Date: 2026-09-18T03:44:30.100226+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "214808602675"
down_revision: Union[str, None] = "eee46e1b1dbf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _set_local_lock_timeout() -> None:
    """Bound lock waits for the current transaction, including after OSS codegen."""
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


_CREDENTIALS_UNIQUE_INDEX = "uq_credentials_id_org"
_CREDENTIALS_UNIQUE_CONSTRAINT = "uq_credentials_id_org"


def _credentials_constraint_exists() -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name AND conrelid = 'credentials'::regclass"),
            {"name": _CREDENTIALS_UNIQUE_CONSTRAINT},
        )
        .scalar()
    )


def _credentials_invalid_index() -> str | None:
    return (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT c.oid::regclass::text FROM pg_class c "
                "JOIN pg_index i ON i.indexrelid = c.oid "
                "WHERE c.relname = :name "
                "AND i.indrelid = 'credentials'::regclass "
                "AND NOT i.indisvalid"
            ),
            {"name": _CREDENTIALS_UNIQUE_INDEX},
        )
        .scalar()
    )


def _reset_index_timeouts() -> None:
    for statement in ("RESET statement_timeout;", "RESET lock_timeout;"):
        try:
            op.execute(statement)
        except Exception:
            # A failed concurrent statement can leave the transaction aborted.
            # Never replace its useful provider/driver exception with cleanup noise.
            pass


def _build_credentials_unique_index() -> None:
    invalid_leftover = _credentials_invalid_index()
    with op.get_context().autocommit_block():
        try:
            op.execute("SET lock_timeout = '5s';")
            op.execute("SET statement_timeout = '3h';")
            if invalid_leftover:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {invalid_leftover};")
            op.execute(
                f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_CREDENTIALS_UNIQUE_INDEX} "
                "ON credentials (credential_id, organization_id);"
            )
        except Exception:
            _reset_index_timeouts()
            raise
        _reset_index_timeouts()


def _ensure_credentials_unique_constraint() -> None:
    if op.get_bind().dialect.name != "postgresql":
        op.create_unique_constraint(
            _CREDENTIALS_UNIQUE_CONSTRAINT,
            "credentials",
            ["credential_id", "organization_id"],
        )
        return
    if _credentials_constraint_exists():
        return
    _build_credentials_unique_index()
    # Attach in its own transaction so the live-table lock ends before the
    # remaining foundation DDL starts.
    with op.get_context().autocommit_block():
        op.execute("BEGIN")
        try:
            _set_local_lock_timeout()
            op.execute(
                'ALTER TABLE "credentials" ADD CONSTRAINT "uq_credentials_id_org" '
                'UNIQUE USING INDEX "uq_credentials_id_org"'
            )
            op.execute("COMMIT")
        except Exception:
            try:
                op.execute("ROLLBACK")
            except Exception:
                pass
            raise


def upgrade() -> None:
    _ensure_credentials_unique_constraint()
    op.create_table(
        "organization_sms_configs",
        sa.Column("sms_config_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("encrypted_webhook_secret", sa.String(), nullable=False),
        sa.Column(
            "webhook_secret_encrypted_method",
            sa.String(),
            server_default=sa.text("'aes'"),
            nullable=False,
        ),
        sa.Column("daily_ingest_cap", sa.Integer(), server_default=sa.text("100"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.organization_id"]),
        sa.PrimaryKeyConstraint("sms_config_id"),
        sa.UniqueConstraint(
            "sms_config_id",
            "organization_id",
            name="uq_organization_sms_configs_id_org",
        ),
    )
    op.create_index(
        "ix_organization_sms_configs_organization_id",
        "organization_sms_configs",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "uq_org_sms_configs_one_connected",
        "organization_sms_configs",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("mode = 'connected' AND deleted_at IS NULL"),
        sqlite_where=sa.text("mode = 'connected' AND deleted_at IS NULL"),
    )
    op.create_table(
        "organization_phone_numbers",
        sa.Column("phone_number_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("sms_config_id", sa.String(), nullable=False),
        sa.Column("phone_number", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), server_default=sa.text("'twilio'"), nullable=False),
        sa.Column("provider_number_sid", sa.String(), nullable=True),
        sa.Column("previous_sms_url", sa.String(), nullable=True),
        sa.Column("previous_sms_method", sa.String(), nullable=True),
        sa.Column("previous_sms_application_sid", sa.String(), nullable=True),
        sa.Column("credential_id", sa.String(), nullable=True),
        sa.Column("provider_cost_cents", sa.Integer(), nullable=True),
        sa.Column("price_cents", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("quarantined_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["credential_id", "organization_id"],
            ["credentials.credential_id", "credentials.organization_id"],
            name="fk_organization_phone_numbers_credential_org",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.organization_id"]),
        sa.ForeignKeyConstraint(
            ["sms_config_id", "organization_id"],
            [
                "organization_sms_configs.sms_config_id",
                "organization_sms_configs.organization_id",
            ],
            name="fk_organization_phone_numbers_sms_config_org",
        ),
        sa.PrimaryKeyConstraint("phone_number_id"),
    )
    op.create_index(
        "ix_organization_phone_numbers_organization_id",
        "organization_phone_numbers",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_organization_phone_numbers_sms_config_id",
        "organization_phone_numbers",
        ["sms_config_id"],
        unique=False,
    )
    op.create_index(
        "uq_org_phone_numbers_org_number",
        "organization_phone_numbers",
        ["organization_id", "phone_number"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND deleted_at IS NULL"),
        sqlite_where=sa.text("status = 'active' AND deleted_at IS NULL"),
    )
    _set_local_lock_timeout()
    op.add_column("totp_codes", sa.Column("external_message_id", sa.String(), nullable=True))


def downgrade() -> None:
    _set_local_lock_timeout()
    op.drop_column("totp_codes", "external_message_id")
    op.drop_index("uq_org_phone_numbers_org_number", table_name="organization_phone_numbers")
    op.drop_index("ix_organization_phone_numbers_sms_config_id", table_name="organization_phone_numbers")
    op.drop_index("ix_organization_phone_numbers_organization_id", table_name="organization_phone_numbers")
    op.drop_table("organization_phone_numbers")
    op.drop_constraint("uq_credentials_id_org", "credentials", type_="unique")
    op.drop_index("ix_organization_sms_configs_organization_id", table_name="organization_sms_configs")
    op.drop_index("uq_org_sms_configs_one_connected", table_name="organization_sms_configs")
    op.drop_table("organization_sms_configs")
