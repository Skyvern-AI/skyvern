"""add managed SMS config and phone fields

Revision ID: 4909d15f6587
Revises: 1dc949839af2
Create Date: 2026-09-25T17:15:02.857740+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4909d15f6587"
down_revision: Union[str, None] = "1dc949839af2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LOCK_TIMEOUT = "5s"


def _replace_phone_number_index() -> None:
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT phone_number_id FROM ("
                "SELECT phone_number_id, count(*) OVER (PARTITION BY organization_id, phone_number) AS reservation_count "
                "FROM organization_phone_numbers "
                "WHERE status IN ('active', 'provisioning', 'quarantined') AND deleted_at IS NULL "
                ") AS reservations WHERE reservation_count > 1 ORDER BY phone_number_id"
            )
        )
        .scalars()
        .all()
    )
    if duplicates:
        raise RuntimeError(
            f"Duplicate managed phone reservations: {len(duplicates)} rows; phone_number_ids: {duplicates}"
        )
    op.drop_index("uq_org_phone_numbers_org_number", table_name="organization_phone_numbers")
    op.create_index(
        "uq_org_phone_numbers_org_number",
        "organization_phone_numbers",
        ["organization_id", "phone_number"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'provisioning', 'quarantined') AND deleted_at IS NULL"),
        sqlite_where=sa.text("status IN ('active', 'provisioning', 'quarantined') AND deleted_at IS NULL"),
    )


def upgrade() -> None:
    op.get_bind().execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    op.add_column(
        "organization_sms_configs",
        sa.Column("encrypted_signing_token", sa.String(), nullable=True),
    )
    op.add_column(
        "organization_sms_configs",
        sa.Column("signing_token_encrypted_method", sa.String(), nullable=True),
    )
    op.add_column(
        "organization_phone_numbers",
        sa.Column("provisioning_claimed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "uq_org_sms_configs_one_managed",
        "organization_sms_configs",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("mode = 'managed' AND deleted_at IS NULL"),
        sqlite_where=sa.text("mode = 'managed' AND deleted_at IS NULL"),
    )
    _replace_phone_number_index()


def downgrade() -> None:
    op.get_bind().execute(sa.text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    op.drop_index("uq_org_phone_numbers_org_number", table_name="organization_phone_numbers")
    op.create_index(
        "uq_org_phone_numbers_org_number",
        "organization_phone_numbers",
        ["organization_id", "phone_number"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND deleted_at IS NULL"),
        sqlite_where=sa.text("status = 'active' AND deleted_at IS NULL"),
    )
    op.drop_column("organization_phone_numbers", "provisioning_claimed_at")
    op.drop_index("uq_org_sms_configs_one_managed", table_name="organization_sms_configs")
    op.drop_column("organization_sms_configs", "signing_token_encrypted_method")
    op.drop_column("organization_sms_configs", "encrypted_signing_token")
