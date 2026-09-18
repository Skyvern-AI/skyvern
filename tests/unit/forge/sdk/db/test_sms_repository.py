import importlib.util
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from asyncpg.exceptions import UniqueViolationError
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.db.models import (
    CredentialModel,
    OrganizationAuthTokenModel,
    OrganizationPhoneNumberModel,
    OrganizationSMSConfigModel,
    TOTPCodeModel,
)
from skyvern.forge.sdk.db.repositories import organizations as organizations_repository_module
from skyvern.forge.sdk.db.repositories import sms as sms_repository_module
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.schemas.organizations import TwilioCredential
from skyvern.forge.sdk.schemas.totp_codes import OTPType


class _FakeEncryptor:
    async def encrypt(self, plaintext: str, method: object) -> str:
        assert getattr(method, "value", None) == "aes"
        return f"encrypted::{plaintext}"

    async def decrypt(self, ciphertext: str, method: object) -> str:
        assert getattr(method, "value", None) == "aes"
        return ciphertext.removeprefix("encrypted::")


@pytest.mark.parametrize("driver", ["sqlite", "psycopg", "asyncpg", "asyncpg-direct"])
@pytest.mark.parametrize("matches", [True, False])
def test_phone_number_unique_violation_uses_driver_constraint_metadata(driver: str, matches: bool) -> None:
    constraint = "uq_org_phone_numbers_org_number" if matches else "unrelated_unique_constraint"
    original: Exception
    if driver == "sqlite":
        original = sqlite3.IntegrityError(
            "UNIQUE constraint failed: organization_phone_numbers.organization_id, organization_phone_numbers.phone_number"
            if matches
            else "UNIQUE constraint failed: organization_phone_numbers.phone_number_id"
        )
    elif driver == "psycopg":
        original = Exception("duplicate key")
        original.diag = SimpleNamespace(constraint_name=constraint)  # type: ignore[attr-defined]
    else:
        violation = UniqueViolationError("duplicate key")
        violation.constraint_name = constraint
        original = violation if driver == "asyncpg-direct" else Exception("adapted driver error")
        if driver == "asyncpg":
            original.__cause__ = violation
    assert sms_repository_module._is_phone_number_unique_violation(IntegrityError("INSERT", {}, original)) is matches


@pytest.mark.asyncio
async def test_reactivating_phone_with_credential_reports_duplicate(agent_db: AgentDB) -> None:
    config, _ = await agent_db.sms.create_sms_config("o_test", "manual", "secret-token")
    async with agent_db.Session() as session:
        session.add(
            CredentialModel(
                credential_id="cred_test", organization_id="o_test", name="Test", credential_type="password"
            )
        )
        await session.commit()
    original = await agent_db.sms.create_phone_number("o_test", config.sms_config_id, "+14155552671")
    await agent_db.sms.update_phone_number(original.phone_number_id, "o_test", status="disabled")
    await agent_db.sms.create_phone_number("o_test", config.sms_config_id, "+14155552671")
    with pytest.raises(sms_repository_module.PhoneNumberAlreadyExists):
        await agent_db.sms.update_phone_number(
            original.phone_number_id, "o_test", status="active", credential_id="cred_test"
        )
    unchanged = await agent_db.sms.get_phone_number(original.phone_number_id, "o_test")
    assert unchanged is not None and unchanged.status == "disabled" and unchanged.credential_id is None


@pytest.mark.asyncio
async def test_otp_lookup_preserves_exact_legacy_whitespace_identifier(agent_db: AgentDB) -> None:
    identifier = " +14155552671 "
    created = await agent_db.otp.create_otp_code(
        organization_id="o_test",
        totp_identifier=identifier,
        content="123456",
        code="123456",
        otp_type=OTPType.TOTP,
    )
    results = await agent_db.otp.get_otp_codes(organization_id="o_test", totp_identifier=identifier)
    assert [result.totp_code_id for result in results] == [created.totp_code_id]


@pytest.fixture(autouse=True)
def fake_sms_encryptor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sms_repository_module, "encryptor", _FakeEncryptor())


@pytest.mark.asyncio
async def test_twilio_org_credentials_default_to_aes_and_reject_other_methods(
    agent_db: AgentDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(organizations_repository_module, "encryptor", _FakeEncryptor())
    monkeypatch.setattr("skyvern.forge.sdk.db.utils.encryptor", _FakeEncryptor())
    credential = TwilioCredential(account_sid="AC" + "0" * 32, auth_token="TWILIO_AUTH_SENTINEL")

    stored = await agent_db.organizations.replace_org_auth_token(
        "o_test",
        OrganizationAuthTokenType.twilio_credential,
        credential,
    )

    assert stored.credential == credential
    async with agent_db.Session() as session:
        row = await session.scalar(
            select(OrganizationAuthTokenModel).where(
                OrganizationAuthTokenModel.organization_id == "o_test",
                OrganizationAuthTokenModel.token_type == OrganizationAuthTokenType.twilio_credential,
            )
        )
    assert row is not None
    assert row.token == ""
    assert row.encrypted_method == EncryptMethod.AES.value
    assert row.encrypted_token == f"encrypted::{credential.model_dump_json()}"

    with pytest.raises(ValueError, match="AES"):
        await agent_db.organizations.replace_org_auth_token(
            "o_test",
            OrganizationAuthTokenType.twilio_credential,
            credential,
            encrypted_method=cast(EncryptMethod, object()),
        )


@pytest.mark.asyncio
async def test_generic_twilio_auth_token_writers_reject_plaintext(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(organizations_repository_module, "encryptor", _FakeEncryptor())
    monkeypatch.setattr("skyvern.forge.sdk.db.utils.encryptor", _FakeEncryptor())
    credential = TwilioCredential(account_sid="AC" + "0" * 32, auth_token="TWILIO_AUTH_SENTINEL")
    stored = await agent_db.organizations.replace_org_auth_token(
        "o_test", OrganizationAuthTokenType.twilio_credential, credential
    )
    serialized_credential = credential.model_dump_json()

    with pytest.raises(ValueError, match="replace_org_auth_token"):
        await agent_db.organizations.create_org_auth_token(
            "o_test", OrganizationAuthTokenType.twilio_credential, serialized_credential
        )
    with pytest.raises(ValueError, match="replace_org_auth_token"):
        await agent_db.organizations.update_org_auth_token(
            "o_test", OrganizationAuthTokenType.twilio_credential, stored.id, serialized_credential
        )

    async with agent_db.Session() as session:
        rows = (
            await session.scalars(
                select(OrganizationAuthTokenModel).where(
                    OrganizationAuthTokenModel.organization_id == "o_test",
                    OrganizationAuthTokenModel.token_type == OrganizationAuthTokenType.twilio_credential,
                )
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].token == ""
    assert rows[0].encrypted_token == f"encrypted::{serialized_credential}"


@pytest.mark.asyncio
async def test_invalid_sms_config_mode_leaves_no_poisoned_row(agent_db: AgentDB) -> None:
    created, _ = await agent_db.sms.create_sms_config("o_test", "manual", "secret-token")
    with pytest.raises(sms_repository_module.InvalidSMSConfigMode, match="Invalid SMS config mode"):
        await agent_db.sms.create_sms_config("o_test", "bogus", "secret-token")
    assert await agent_db.sms.list_sms_configs("o_test") == [created]
    assert await agent_db.sms.get_sms_config(created.sms_config_id, "o_test") == created


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update", "disable", "delete"])
async def test_sms_timestamp_writes_are_naive_utc(agent_db: AgentDB, db_engine: Any, operation: str) -> None:
    writes: list[dict[str, datetime]] = []

    def capture_timestamps(_conn: Any, _cursor: Any, _sql: Any, _parameters: Any, context: Any, _many: Any) -> None:
        if context.isinsert or context.isupdate:
            for parameters in context.compiled_parameters:
                writes.append({key: value for key, value in parameters.items() if isinstance(value, datetime)})

    event.listen(db_engine.sync_engine, "before_cursor_execute", capture_timestamps)
    config, _ = await agent_db.sms.create_sms_config("o_test", "manual", "secret-token")
    phone = await agent_db.sms.create_phone_number("o_test", config.sms_config_id, "+14155552671")
    timestamp = datetime(2026, 9, 10, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    expected = datetime(2026, 9, 10, 9, 30)
    if operation == "update":
        await agent_db.sms.update_phone_number(phone.phone_number_id, "o_test", quarantined_until=timestamp)
    elif operation == "disable":
        await agent_db.sms.update_phone_number(phone.phone_number_id, "o_test", status="disabled")
    elif operation == "delete":
        await agent_db.sms.delete_sms_config(
            (await agent_db.sms.create_sms_config("o_test", "manual", "other-secret"))[0].sms_config_id, "o_test"
        )

    assert {"created_at", "modified_at"} <= {key for write in writes for key in write}
    for write in writes:
        for key, value in write.items():
            assert value.tzinfo is None, f"{operation}: {key} must be naive UTC before SQL encoding"
    async with agent_db.Session() as session:
        rows = [
            *(await session.scalars(select(OrganizationSMSConfigModel))).all(),
            *(await session.scalars(select(OrganizationPhoneNumberModel))).all(),
        ]
        for row in rows:
            for column in row.__table__.columns:
                value = getattr(row, column.name)
                if isinstance(value, datetime):
                    assert value.tzinfo is None, f"{column.name} must persist as naive UTC"
    if operation == "update":
        loaded = await agent_db.sms.get_phone_number(phone.phone_number_id, "o_test")
        assert loaded is not None and loaded.quarantined_until == expected
        await agent_db.sms.update_phone_number(phone.phone_number_id, "o_test", quarantined_until=expected)
        await agent_db.sms.update_phone_number(phone.phone_number_id, "o_test", quarantined_until=None)
        loaded = await agent_db.sms.get_phone_number(phone.phone_number_id, "o_test")
        assert loaded is not None and loaded.quarantined_until is None
    if operation == "delete":
        assert any("deleted_at" in write for write in writes)


@pytest.mark.asyncio
async def test_sms_config_create_get_list_and_delete(agent_db: AgentDB) -> None:
    created, plaintext_secret = await agent_db.sms.create_sms_config(
        organization_id="o_test",
        mode="manual",
        webhook_secret="secret-token",
        daily_ingest_cap=25,
    )

    assert plaintext_secret == "secret-token"
    assert created.organization_id == "o_test"
    assert created.mode == "manual"
    assert created.daily_ingest_cap == 25
    assert not hasattr(created, "webhook_secret")
    assert not hasattr(created, "encrypted_webhook_secret")

    async with agent_db.Session() as session:
        stored = await session.scalar(
            select(OrganizationSMSConfigModel).where(OrganizationSMSConfigModel.sms_config_id == created.sms_config_id)
        )
    assert stored is not None
    assert stored.encrypted_webhook_secret == "encrypted::secret-token"
    assert stored.webhook_secret_encrypted_method == "aes"

    loaded = await agent_db.sms.get_sms_config_with_secret(
        organization_id="o_test", sms_config_id=created.sms_config_id
    )
    assert loaded == (created, "secret-token")
    assert await agent_db.sms.get_sms_config_with_secret_for_inbound(sms_config_id=created.sms_config_id) == loaded
    assert (
        await agent_db.sms.get_sms_config_with_secret(organization_id="o_other", sms_config_id=created.sms_config_id)
        is None
    )
    assert await agent_db.sms.get_sms_config(created.sms_config_id, "o_test") == created
    assert await agent_db.sms.list_sms_configs("o_test") == [created]

    await agent_db.sms.delete_sms_config(created.sms_config_id, "o_test")

    assert (
        await agent_db.sms.get_sms_config_with_secret(organization_id="o_test", sms_config_id=created.sms_config_id)
        is None
    )
    assert await agent_db.sms.get_sms_config_with_secret_for_inbound(sms_config_id=created.sms_config_id) is None
    assert await agent_db.sms.get_sms_config(created.sms_config_id, "o_test") is None
    assert await agent_db.sms.get_sms_config(created.sms_config_id, "o_test", include_deleted=True) == created
    assert await agent_db.sms.list_sms_configs("o_test") == []


@pytest.mark.asyncio
async def test_create_connected_sms_config_conflict_returns_existing_winner(agent_db: AgentDB) -> None:
    winner, winner_secret = await agent_db.sms.create_sms_config(
        organization_id="o_test",
        mode="connected",
        webhook_secret="winner-secret",
    )

    conflicted, conflicted_secret = await agent_db.sms.create_sms_config(
        organization_id="o_test",
        mode="connected",
        webhook_secret="loser-secret",
    )

    assert winner_secret == "winner-secret"
    assert conflicted == winner
    assert conflicted_secret == "winner-secret"
    assert await agent_db.sms.get_connected_sms_config("o_test") == (winner, "winner-secret")
    assert await agent_db.sms.list_sms_configs("o_test") == [winner]


@pytest.mark.asyncio
async def test_phone_number_invalid_status_leaves_row_unchanged(agent_db: AgentDB) -> None:
    config, _ = await agent_db.sms.create_sms_config(
        organization_id="o_test", mode="manual", webhook_secret="secret-token"
    )
    created = await agent_db.sms.create_phone_number(
        organization_id="o_test", sms_config_id=config.sms_config_id, phone_number="+14155552671"
    )

    with pytest.raises(sms_repository_module.InvalidPhoneNumberStatus, match="Invalid phone number status"):
        await agent_db.sms.update_phone_number(created.phone_number_id, "o_test", status="bogus")

    assert await agent_db.sms.get_phone_number(created.phone_number_id, "o_test") == created
    assert await agent_db.sms.list_phone_numbers("o_test") == [created]


@pytest.mark.asyncio
async def test_phone_number_uniqueness_lookup_updates_and_active_count(agent_db: AgentDB) -> None:
    config, _ = await agent_db.sms.create_sms_config(
        organization_id="o_test",
        mode="manual",
        webhook_secret="secret-token",
    )
    other_config, _ = await agent_db.sms.create_sms_config(
        organization_id="o_other",
        mode="manual",
        webhook_secret="other-secret-token",
    )
    async with agent_db.Session() as session:
        session.add(
            CredentialModel(
                credential_id="cred_other",
                organization_id="o_other",
                name="Other credential",
                credential_type="password",
            )
        )
        await session.commit()
    created = await agent_db.sms.create_phone_number(
        organization_id="o_test",
        sms_config_id=config.sms_config_id,
        phone_number="+14155552671",
        provider_number_sid="PN_original",
        previous_sms_url="https://example.test/original",
        previous_sms_method="GET",
        previous_sms_application_sid="AP_original",
    )

    assert await agent_db.sms.get_phone_number_by_number("+14155552671", "o_test") == created
    assert await agent_db.sms.get_phone_number_by_number("+14155552671", "o_other") is None

    other_org_number = await agent_db.sms.create_phone_number(
        organization_id="o_other",
        sms_config_id=other_config.sms_config_id,
        phone_number="+14155552671",
    )
    assert await agent_db.sms.get_phone_number_by_number("+14155552671", "o_other") == other_org_number

    with pytest.raises(ValueError, match="does not belong"):
        await agent_db.sms.create_phone_number(
            organization_id="o_test",
            sms_config_id=other_config.sms_config_id,
            phone_number="+14155552672",
        )

    with pytest.raises(ValueError, match="Credential does not belong"):
        await agent_db.sms.create_phone_number(
            organization_id="o_test",
            sms_config_id=config.sms_config_id,
            phone_number="+14155552673",
            credential_id="cred_other",
        )

    with pytest.raises(sms_repository_module.PhoneNumberAlreadyExists, match="already registered"):
        await agent_db.sms.create_phone_number(
            organization_id="o_test",
            sms_config_id=config.sms_config_id,
            phone_number="+14155552671",
        )

    updated = await agent_db.sms.update_phone_number(
        created.phone_number_id,
        "o_test",
        status="disabled",
        provider_number_sid="PN_refreshed",
        previous_sms_method="POST",
        previous_sms_application_sid="AP_refreshed",
    )
    assert updated is not None
    assert updated.status == "disabled"
    assert updated.provider_number_sid == "PN_refreshed"
    assert updated.previous_sms_method == "POST"
    assert updated.previous_sms_application_sid == "AP_refreshed"

    replacement = await agent_db.sms.create_phone_number(
        organization_id="o_test",
        sms_config_id=config.sms_config_id,
        phone_number="+14155552671",
    )
    assert replacement.status == "active"
    with pytest.raises(sms_repository_module.PhoneNumberAlreadyExists, match="already registered"):
        await agent_db.sms.update_phone_number(created.phone_number_id, "o_test", status="active")
    assert await agent_db.sms.count_active_phone_numbers_for_config(config.sms_config_id, "o_test") == 1
    assert await agent_db.sms.count_active_phone_numbers_for_config(config.sms_config_id, "o_other") == 0
    with pytest.raises(ValueError, match="Credential does not belong"):
        await agent_db.sms.update_phone_number(
            replacement.phone_number_id,
            "o_test",
            credential_id="cred_other",
        )
    assert await agent_db.sms.count_active_phone_numbers_for_config(other_config.sms_config_id, "o_other") == 1
    assert await agent_db.sms.get_phone_number_by_number("+1 (415) 555-2671", "o_test") == replacement
    assert await agent_db.sms.get_phone_number_by_number("+1-EXT-415-555-2671", "o_test") is None


@pytest.mark.asyncio
async def test_delete_sms_config_rejects_active_phone_references(agent_db: AgentDB) -> None:
    config, _ = await agent_db.sms.create_sms_config("o_test", "manual", "secret-token")
    phone = await agent_db.sms.create_phone_number("o_test", config.sms_config_id, "+14155552671")
    with pytest.raises(sms_repository_module.SMSConfigHasActivePhoneNumbers, match="active phone"):
        await agent_db.sms.delete_sms_config(config.sms_config_id, "o_test")
    assert await agent_db.sms.get_sms_config(config.sms_config_id, "o_test") == config
    await agent_db.sms.update_phone_number(phone.phone_number_id, "o_test", status="disabled")
    await agent_db.sms.delete_sms_config(config.sms_config_id, "o_test")
    assert await agent_db.sms.get_sms_config(config.sms_config_id, "o_test") is None


@pytest.mark.asyncio
async def test_phone_number_create_normalizes_and_validates_e164(agent_db: AgentDB) -> None:
    config, _ = await agent_db.sms.create_sms_config(
        organization_id="o_test",
        mode="manual",
        webhook_secret="secret-token",
    )

    created = await agent_db.sms.create_phone_number(
        organization_id="o_test",
        sms_config_id=config.sms_config_id,
        phone_number="+1 (415) 555-2671",
    )

    assert created.phone_number == "+14155552671"
    with pytest.raises(ValueError, match="country code"):
        await agent_db.sms.create_phone_number(
            organization_id="o_test",
            sms_config_id=config.sms_config_id,
            phone_number="4155552672",
        )
    with pytest.raises(ValueError, match="country code"):
        await agent_db.sms.create_phone_number(
            organization_id="o_test",
            sms_config_id=config.sms_config_id,
            phone_number="+1-EXT-415-555-2673",
        )


@pytest.mark.parametrize(
    ("stored_identifier", "lookup_identifier"),
    [
        pytest.param("4155552671", "+14155552671", id="legacy-bare-row"),
        pytest.param("+14155552671", "4155552671", id="e164-row-with-legacy-lookup"),
    ],
)
@pytest.mark.asyncio
async def test_otp_repository_does_not_cross_match_country_ambiguous_phone_identifiers(
    agent_db: AgentDB,
    stored_identifier: str,
    lookup_identifier: str,
) -> None:
    async with agent_db.Session() as session:
        session.add(
            TOTPCodeModel(
                totp_code_id="totp_phone_identifier",
                organization_id="o_test",
                totp_identifier=stored_identifier,
                content="123456",
                code="123456",
                otp_type="totp",
                parse_status="parsed",
            )
        )
        await session.commit()

    results = await agent_db.otp.get_otp_codes(
        organization_id="o_test",
        totp_identifier=lookup_identifier,
    )

    assert results == []


@pytest.mark.asyncio
async def test_if_new_otp_creators_return_none_for_duplicate_external_message_id(
    agent_db: AgentDB,
    caplog: pytest.LogCaptureFixture,
) -> None:
    created = await agent_db.otp.create_otp_code_if_new(
        organization_id="o_test",
        totp_identifier="ID_SENTINEL",
        content="BODY_SENTINEL",
        code="CODE_SENTINEL",
        otp_type=OTPType.TOTP,
        source="twilio",
        external_message_id="MSG_PARSED_SENTINEL",
    )
    raw_created = await agent_db.otp.create_raw_otp_code_if_new(
        organization_id="o_test",
        totp_identifier="ID_SENTINEL",
        content="BODY_SENTINEL",
        source="twilio",
        external_message_id="MSG_RAW_SENTINEL",
    )
    caplog.clear()
    duplicate = await agent_db.otp.create_otp_code_if_new(
        organization_id="o_test",
        totp_identifier="ID_SENTINEL",
        content="654321",
        code="654321",
        otp_type=OTPType.TOTP,
        source="twilio",
        external_message_id="MSG_PARSED_SENTINEL",
    )
    raw_duplicate = await agent_db.otp.create_raw_otp_code_if_new(
        organization_id="o_test",
        totp_identifier="ID_SENTINEL",
        content="BODY_SENTINEL",
        source="twilio",
        external_message_id="MSG_RAW_SENTINEL",
    )

    assert created is not None
    assert duplicate is None
    assert raw_created is not None
    assert raw_duplicate is None
    assert not any(sentinel in caplog.text for sentinel in ("ID_SENTINEL", "BODY_SENTINEL", "CODE_SENTINEL"))


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [False, True], ids=["parsed", "raw"])
async def test_if_new_otp_creators_reraise_non_dedupe_integrity_errors(
    agent_db: AgentDB,
    monkeypatch: pytest.MonkeyPatch,
    raw: bool,
) -> None:
    monkeypatch.setattr(
        AsyncSession,
        "commit",
        AsyncMock(side_effect=IntegrityError("INSERT", {}, Exception("foreign key constraint failed"))),
    )
    kwargs = {
        "organization_id": "o_test",
        "totp_identifier": "+14155552671",
        "content": "123456",
        "task_id": "tsk_missing",
        "source": "twilio",
        "external_message_id": f"SM_invalid_fk_{raw}",
    }

    with pytest.raises(IntegrityError) as exc_info:
        if raw:
            await agent_db.otp.create_raw_otp_code_if_new(**kwargs)
        else:
            await agent_db.otp.create_otp_code_if_new(
                **kwargs,
                code="123456",
                otp_type=OTPType.TOTP,
            )
    assert exc_info.value.hide_parameters


@pytest.mark.asyncio
async def test_count_otp_codes_since_filters_source_and_counts_raw_rows(
    agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    threshold = datetime.now(timezone.utc) - timedelta(hours=1)
    common = {
        "organization_id": "o_test",
        "totp_identifier": "+14155552671",
    }
    await agent_db.otp.create_otp_code(
        **common,
        content="123456",
        code="123456",
        otp_type=OTPType.TOTP,
        source="twilio",
    )
    await agent_db.otp.create_otp_code(
        **common,
        content="654321",
        code="654321",
        otp_type=OTPType.TOTP,
        source="twilio",
    )
    await agent_db.otp.create_raw_otp_code(
        **common,
        content="Your code is 111222",
        source="twilio",
    )
    await agent_db.otp.create_otp_code(
        **common,
        content="333444",
        code="333444",
        otp_type=OTPType.TOTP,
        source="email",
    )
    async with agent_db.Session() as session:
        session.add(
            TOTPCodeModel(
                totp_code_id="totp_before_threshold",
                organization_id="o_test",
                totp_identifier="+14155552671",
                content="999000",
                code="999000",
                otp_type="totp",
                parse_status="parsed",
                source="twilio",
                created_at=threshold - timedelta(seconds=1),
            )
        )
        await session.commit()

    original_scalar = AsyncSession.scalar

    async def scalar_with_naive_timestamp(session: AsyncSession, statement: Any, **kwargs: Any) -> Any:
        assert statement.compile().params["created_at_1"] == threshold.replace(tzinfo=None)
        return await original_scalar(session, statement, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", scalar_with_naive_timestamp)
    assert (
        await agent_db.otp.count_otp_codes_since(
            **common,
            created_after=threshold,
            source="twilio",
        )
        == 3
    )
    assert (
        await agent_db.otp.count_otp_codes_since(
            **common,
            created_after=threshold,
            source="email",
        )
        == 1
    )
    assert (
        await agent_db.otp.count_otp_codes_since(
            **common,
            created_after=threshold,
        )
        == 4
    )


@pytest.mark.asyncio
async def test_otp_repository_matches_seven_digit_phone_identifier_candidates(agent_db: AgentDB) -> None:
    async with agent_db.Session() as session:
        session.add_all(
            [
                TOTPCodeModel(
                    totp_code_id="totp_formatted_label",
                    organization_id="o_test",
                    totp_identifier="1234-567",
                    content="123456",
                    code="123456",
                    otp_type="totp",
                    parse_status="parsed",
                ),
                TOTPCodeModel(
                    totp_code_id="totp_digits_only",
                    organization_id="o_test",
                    totp_identifier="1234567",
                    content="654321",
                    code="654321",
                    otp_type="totp",
                    parse_status="parsed",
                ),
            ]
        )
        await session.commit()

    results = await agent_db.otp.get_otp_codes(
        organization_id="o_test",
        totp_identifier="1234-567",
    )

    assert {result.totp_identifier for result in results} == {"1234-567", "1234567"}


def _load_sms_migration() -> Any:
    migrations = list((Path(__file__).resolve().parents[5] / "alembic/versions").glob("*_add_sms_2fa_foundation.py"))
    assert len(migrations) == 1, f"Expected exactly one SMS foundation migration, found {len(migrations)}: {migrations}"
    migration_path = migrations[0]
    spec = importlib.util.spec_from_file_location("sms_foundation_migration", migration_path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


class _FakeMigrationResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar(self) -> object:
        return self.value


class _FakeMigrationBind:
    class _Dialect:
        name = "postgresql"

    dialect = _Dialect()

    def __init__(self, *, constraint_exists: bool = False, invalid_index: str | None = None) -> None:
        self.constraint_exists = constraint_exists
        self.invalid_index = invalid_index

    def execute(self, statement: object, _parameters: object = None) -> _FakeMigrationResult:
        sql = str(statement)
        if "FROM pg_constraint" in sql:
            return _FakeMigrationResult(1 if self.constraint_exists else None)
        if "FROM pg_class" in sql:
            return _FakeMigrationResult(self.invalid_index)
        return _FakeMigrationResult(None)


class _FakeMigrationContext:
    # Track requested context placement only; this does not exercise PostgreSQL transactions.
    in_autocommit = False

    @contextmanager
    def autocommit_block(self):
        assert not self.in_autocommit
        self.in_autocommit = True
        try:
            yield
        finally:
            self.in_autocommit = False


class _FakeMigrationOp:
    def __init__(
        self,
        *,
        constraint_exists: bool = False,
        invalid_index: str | None = None,
        failure_statement: str | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.bind = _FakeMigrationBind(constraint_exists=constraint_exists, invalid_index=invalid_index)
        self.context = _FakeMigrationContext()
        self.statements: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.failure_statement = failure_statement
        self.failure = failure or RuntimeError("migration operation failed")

    def get_bind(self) -> _FakeMigrationBind:
        return self.bind

    def get_context(self) -> _FakeMigrationContext:
        return self.context

    def execute(self, statement: object) -> None:
        sql = str(statement)
        if "INDEX CONCURRENTLY" in sql:
            assert self.context.in_autocommit, "Concurrent index DDL must request an autocommit block"
        self.statements.append(sql)
        if self.failure_statement and self.failure_statement in sql:
            raise self.failure

    def create_unique_constraint(self, name: str, _table: str, _columns: list[str]) -> None:
        self.calls.append(("create_unique_constraint", name))

    def create_table(self, name: str, *_args: object, **_kwargs: object) -> None:
        self.calls.append(("create_table", name))

    def create_index(self, name: str, _table: str, _columns: list[str], **_kwargs: object) -> None:
        self.calls.append(("create_index", name))

    def add_column(self, table: str, _column: object) -> None:
        self.calls.append(("add_column", table))

    def drop_column(self, table: str, column: str) -> None:
        self.calls.append(("drop_column", f"{table}.{column}"))

    def drop_index(self, name: str, **_kwargs: object) -> None:
        self.calls.append(("drop_index", name))

    def drop_table(self, name: str) -> None:
        self.calls.append(("drop_table", name))

    def drop_constraint(self, name: str, _table: str, **_kwargs: object) -> None:
        self.calls.append(("drop_constraint", name))


def test_sms_migration_upgrade_builds_credentials_constraint_from_concurrent_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_sms_migration()
    fake_op = _FakeMigrationOp()
    monkeypatch.setattr(migration, "op", fake_op)

    migration.upgrade()

    normalized = " ".join(" ".join(fake_op.statements).split())
    assert "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_credentials_id_org" in normalized
    assert (
        'ALTER TABLE "credentials" ADD CONSTRAINT "uq_credentials_id_org" UNIQUE USING INDEX "uq_credentials_id_org"'
        in (normalized)
    )
    assert "SET LOCAL lock_timeout = '5s'" in normalized
    assert not any(call[0] == "create_unique_constraint" for call in fake_op.calls)


def test_sms_migration_upgrade_rebuilds_invalid_credentials_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_sms_migration()
    fake_op = _FakeMigrationOp(invalid_index="uq_credentials_id_org")
    monkeypatch.setattr(migration, "op", fake_op)

    migration.upgrade()

    statements = [statement.replace("\n", " ") for statement in fake_op.statements]
    drop_position = next(index for index, statement in enumerate(statements) if "DROP INDEX CONCURRENTLY" in statement)
    create_position = next(
        index for index, statement in enumerate(statements) if "CREATE UNIQUE INDEX CONCURRENTLY" in statement
    )
    assert drop_position < create_position


def test_sms_migration_upgrade_preserves_index_timeout_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_sms_migration()
    timeout = RuntimeError("canceling statement due to lock timeout")
    fake_op = _FakeMigrationOp(failure_statement="CREATE UNIQUE INDEX", failure=timeout)
    monkeypatch.setattr(migration, "op", fake_op)

    with pytest.raises(RuntimeError) as exc_info:
        migration.upgrade()

    assert exc_info.value is timeout


def test_sms_migration_upgrade_preserves_attach_timeout_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_sms_migration()
    timeout = RuntimeError("canceling statement due to lock timeout")
    fake_op = _FakeMigrationOp(failure_statement='ALTER TABLE "credentials"', failure=timeout)
    monkeypatch.setattr(migration, "op", fake_op)

    with pytest.raises(RuntimeError) as exc_info:
        migration.upgrade()

    assert exc_info.value is timeout
    assert "ROLLBACK" in fake_op.statements


def test_sms_migration_upgrade_reuses_attached_credentials_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_sms_migration()
    fake_op = _FakeMigrationOp(constraint_exists=True)
    monkeypatch.setattr(migration, "op", fake_op)

    migration.upgrade()

    assert not any("CREATE UNIQUE INDEX" in statement for statement in fake_op.statements)
    assert not any("ADD CONSTRAINT" in statement for statement in fake_op.statements)


def test_sms_migration_downgrade_drops_credentials_constraint_without_index_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_sms_migration()
    fake_op = _FakeMigrationOp()
    monkeypatch.setattr(migration, "op", fake_op)

    migration.downgrade()

    assert ("drop_constraint", "uq_credentials_id_org") in fake_op.calls
    assert ("drop_index", "uq_credentials_id_org") not in fake_op.calls
