import asyncio
import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import QueuePool

from skyvern.forge import request_logging
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import (
    Base,
    BitwardenLoginCredentialParameterModel,
    OnePasswordCredentialParameterModel,
    OrganizationPhoneNumberModel,
    OrganizationSMSConfigModel,
    TOTPCodeModel,
)
from skyvern.forge.sdk.db.repositories.sms import SMSRepository
from skyvern.forge.sdk.db.repositories.workflow_parameters import WorkflowParametersRepository
from skyvern.forge.sdk.db.utils import convert_to_bitwarden_login_credential_parameter
from skyvern.forge.sdk.routes import credentials, run_blocks, sms_inbound, twilio_integration
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    CredentialItem,
    CredentialType,
    CredentialVaultType,
    NonEmptyPasswordCredential,
    PasswordCredential,
    TotpType,
)
from skyvern.forge.sdk.schemas.organizations import CreateTwilioCredentialRequest, Organization, TwilioCredential
from skyvern.forge.sdk.schemas.sms import (
    DisablePhoneNumberRequest,
    EnablePhoneNumberRequest,
    OrganizationPhoneNumber,
    SMSConfig,
)
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants
from skyvern.forge.sdk.services.credential.skyvern_credential_vault_service import SkyvernCredentialVaultService
from skyvern.forge.sdk.services.twilio_service import TwilioApiError, build_inbound_sms_url, compute_twilio_signature
from skyvern.forge.sdk.workflow import context_manager as cm
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.parameter import (
    BitwardenLoginCredentialParameter,
    OnePasswordCredentialParameter,
)
from skyvern.forge.sdk.workflow.models.tags import CallerType
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.credential_type import CredentialType as LoginCredentialType
from skyvern.schemas.run_blocks import LoginRequest
from skyvern.schemas.workflows import (
    BitwardenLoginCredentialParameterYAML,
    OnePasswordCredentialParameterYAML,
    WorkflowDefinitionYAML,
    WorkflowStatus,
)
from skyvern.services import otp_service
from tests.unit.forge.sdk.db import conftest as db_fixtures

agent_db = db_fixtures.agent_db
db_engine = db_fixtures.db_engine
ORG = SimpleNamespace(organization_id="o_test")
ACCOUNT_SID = "AC" + "0123456789abcdef" * 2
OTHER_ACCOUNT_SID = "AC" + "fedcba9876543210" * 2
API_KEY_SID = "SK" + "0123456789abcdef" * 2


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("prefix", ["/v1", "/api/v1"])
@pytest.mark.parametrize("path", ["integrations/twilio", "integrations/twilio/credentials", "sms", "sms/inbound/test"])
@pytest.mark.parametrize("content_type", ["application/json", "application/x-www-form-urlencoded", ""])
def test_twilio_middleware_hides_malformed_secret_bodies(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    method: str,
    prefix: str,
    path: str,
    content_type: str,
) -> None:
    api = FastAPI()
    api.add_middleware(request_logging.RequestLoggingMiddleware)
    monkeypatch.setattr(request_logging.settings, "LOG_RAW_API_REQUESTS", True)

    @api.api_route("/{path:path}", methods=[method])
    async def reject(request: Request) -> Response:
        return Response(await request.body(), status_code=400, media_type="text/plain")

    body = '{"credential":{"auth_token":"AUTH_SENTINEL"' if content_type == "application/json" else "Body=OTP_SENTINEL"
    with caplog.at_level(logging.INFO), TestClient(api) as client:
        response = client.request(method, f"{prefix}/{path}", content=body, headers={"content-type": content_type})
    assert response.status_code == 400
    assert "api.raw_request" in caplog.text
    assert "SENTINEL" not in caplog.text
    assert request_logging.REDACTED in caplog.text
    print(f"R1: {method} {prefix}/{path} {content_type or 'missing type'}; captured logs contain no sentinel")
    print(caplog.text)


@pytest.mark.parametrize("model", [CreateTwilioCredentialRequest, twilio_integration.TwilioCredentialResponse])
@pytest.mark.parametrize("shape", ["nested-missing-field", "invalid-wrapper-field", "invalid-wrapper"])
def test_twilio_route_models_hide_validation_inputs(model: type[BaseModel], shape: str) -> None:
    secret = {"auth_token": "AUTH_SENTINEL", "api_key_secret": "KEY_SENTINEL"}
    payload = {"credential": secret if shape == "nested-missing-field" else list(secret.values())}
    with pytest.raises(ValidationError) as caught:
        model.model_validate([payload] if shape == "invalid-wrapper" else payload)
    error = caught.value
    for rendered in (str(error), repr(error), str(error.errors()), error.json()):
        assert "AUTH_SENTINEL" not in rendered
        assert "KEY_SENTINEL" not in rendered


@pytest.mark.parametrize("model", [CreateTwilioCredentialRequest, twilio_integration.TwilioCredentialResponse])
def test_twilio_route_models_hide_malformed_json_inputs(model: type[BaseModel]) -> None:
    with pytest.raises(ValidationError) as caught:
        model.model_validate_json('{"credential":{"auth_token":"AUTH_SENTINEL","api_key_secret":"KEY_SENTINEL"}')
    for rendered in (str(caught.value), repr(caught.value), str(caught.value.errors()), caught.value.json()):
        assert "AUTH_SENTINEL" not in rendered
        assert "KEY_SENTINEL" not in rendered


def _credential() -> TwilioCredential:
    return TwilioCredential(account_sid=ACCOUNT_SID, auth_token="auth-token-secret")


def _credential_token() -> SimpleNamespace:
    return SimpleNamespace(credential=_credential())


def _config(mode: str = "connected") -> SMSConfig:
    return SMSConfig(
        sms_config_id="smsc_test",
        organization_id=ORG.organization_id,
        mode=mode,
        daily_ingest_cap=100,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _registered_phone(config: SMSConfig, **overrides: object) -> OrganizationPhoneNumber:
    values: dict[str, object] = {
        "phone_number": "+14155550123",
        "phone_number_id": "pn_existing",
        "provider": "twilio",
        "organization_id": ORG.organization_id,
        "sms_config_id": config.sms_config_id,
        "provider_number_sid": "PN0123456789abcdef0123456789abcdef",
        "previous_sms_url": "https://customer.example/sms",
        "previous_sms_method": "GET",
        "previous_sms_application_sid": "AP_customer",
        "credential_id": None,
        "status": "active",
        "created_at": config.created_at,
        "modified_at": config.created_at,
    }
    values.update(overrides)
    return OrganizationPhoneNumber.model_validate(values)


def _database(*, token: SimpleNamespace | None = None) -> SimpleNamespace:
    organizations = SimpleNamespace(
        get_valid_org_auth_token=AsyncMock(return_value=token),
        replace_org_auth_token=AsyncMock(),
        invalidate_org_auth_tokens=AsyncMock(),
    )
    credentials = SimpleNamespace(get_credential=AsyncMock(return_value=SimpleNamespace(credential_id="cred_test")))
    sms = create_autospec(SMSRepository, instance=True, spec_set=True)
    sms.create_sms_config.return_value = (_config(), "webhook-secret")
    for method in (
        "get_connected_sms_config",
        "get_sms_config_with_secret",
        "get_sms_config_with_secret_for_inbound",
        "get_sms_config",
        "get_phone_number_by_number",
        "get_phone_number",
        "update_phone_number",
    ):
        getattr(sms, method).return_value = None
    sms.list_sms_configs.return_value = []
    sms.list_phone_numbers.return_value = []
    sms.count_active_phone_numbers_for_config.return_value = 0
    sms.count_active_twilio_phone_numbers_for_config.return_value = 0
    return SimpleNamespace(organizations=organizations, credentials=credentials, sms=sms)


def _patch_client(monkeypatch: pytest.MonkeyPatch) -> tuple[SimpleNamespace, Mock]:
    client = SimpleNamespace(
        get_account=AsyncMock(return_value={"status": "active"}),
        list_incoming_phone_numbers=AsyncMock(return_value=[]),
        get_incoming_phone_number=AsyncMock(),
        update_inbound_sms_config=AsyncMock(),
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr(twilio_integration, "build_client_from_credential", factory)
    return client, factory


def _provider_number(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "sid": "PN0123456789abcdef0123456789abcdef",
        "phone_number": "+14155550123",
        "sms_url": "",
        "sms_method": "POST",
        "sms_application_sid": "",
        "capabilities": {"SMS": True},
    }
    result.update(overrides)
    return result


def _apply_provider_routing_then_raise(provider: dict[str, object], error: BaseException) -> object:
    async def apply_then_raise(*args: object, **kwargs: object) -> None:
        provider["sms_url"] = args[1]
        provider["sms_method"] = args[2]
        provider["sms_application_sid"] = args[3]
        raise error

    return apply_then_raise


@pytest.fixture(autouse=True)
def enable_twilio_sms_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        twilio_integration.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(TWILIO_SMS_2FA_ENABLED=True),
    )


@pytest.mark.asyncio
async def test_postgres_lifecycle_permit_reserves_pool_connection_for_repository_work() -> None:
    class FakePool(QueuePool):
        def __init__(self) -> None:
            super().__init__(lambda: None, pool_size=2, max_overflow=0)
            self.connections = asyncio.Semaphore(2)
            self.active = 0
            self.maximum = 0

    pool = FakePool()
    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        pool=pool,
    )
    database_locks: dict[str, asyncio.Lock] = {}
    progress = 0

    class FakeSession:
        def get_bind(self) -> SimpleNamespace:
            return engine

        async def connection(self) -> None:
            pass

        async def scalar(self, statement: object, params: object) -> bool:
            lock = database_locks.setdefault(str(params["lock_key"]), asyncio.Lock())
            if lock.locked():
                return False
            await lock.acquire()
            return True

        async def execute(self, statement: object, params: object) -> None:
            lock_key = str(params["lock_key"])  # type: ignore[index]
            lock = database_locks.setdefault(lock_key, asyncio.Lock())
            if "pg_advisory_unlock" in str(statement):
                lock.release()
            else:
                await lock.acquire()

        async def __aenter__(self) -> "FakeSession":
            await pool.connections.acquire()
            pool.active += 1
            pool.maximum = max(pool.maximum, pool.active)
            return self

        async def __aexit__(self, *args: object) -> None:
            pool.active -= 1
            pool.connections.release()

    class FakeSessionFactory:
        kw = {"bind": SimpleNamespace(sync_engine=engine)}

        def __call__(self) -> FakeSession:
            return FakeSession()

    repository = SMSRepository(FakeSessionFactory())

    async def worker(index: int) -> None:
        nonlocal progress
        async with repository.lifecycle_lock(f"org-{index}", f"+14155550{index:03d}"):
            async with repository.Session():
                progress += 1
                await asyncio.sleep(0.01)

    await asyncio.wait_for(asyncio.gather(worker(1), worker(2)), timeout=1)
    assert progress == 2
    assert pool.maximum == 2


@pytest.mark.asyncio
async def test_update_phone_number_persists_provider_fields_and_quarantine(
    sqlite_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SMSRepository(async_sessionmaker(sqlite_engine, expire_on_commit=False))
    monkeypatch.setattr("skyvern.forge.sdk.workflow.secret_encryption.settings.ENABLE_ENCRYPTION", True)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.secret_encryption.settings.ENCRYPTOR_AES_SECRET_KEY", "test-secret")
    async with repository.Session() as session:
        session.add(
            OrganizationSMSConfigModel(
                sms_config_id="smsc_update",
                organization_id="o_test",
                mode="connected",
                encrypted_webhook_secret="unused",
                webhook_secret_encrypted_method="aes",
            )
        )
        session.add(
            OrganizationPhoneNumberModel(
                phone_number_id="pn_update",
                organization_id="o_test",
                sms_config_id="smsc_update",
                phone_number="+14155550123",
                provider="skyvern",
                status="disabled",
            )
        )
        await session.commit()
    quarantine_until = datetime(2026, 8, 20, 12, tzinfo=UTC)
    updated = await repository.update_phone_number(
        "pn_update",
        "o_test",
        status="quarantined",
        provider="twilio",
        sms_config_id="smsc_update",
        provider_number_sid="PN_UPDATED",
        previous_sms_url="https://customer.example/sms",
        previous_sms_method="POST",
        previous_sms_application_sid="AP_UPDATED",
        credential_id=None,
        quarantined_until=quarantine_until,
    )
    assert updated is not None
    assert updated.provider == "twilio"
    assert updated.previous_sms_url != "https://customer.example/sms"
    assert updated.quarantined_until is not None
    async with repository.Session() as session:
        row = await session.scalar(
            select(OrganizationPhoneNumberModel).where(OrganizationPhoneNumberModel.phone_number_id == "pn_update")
        )
    assert row is not None
    assert row.previous_sms_url == updated.previous_sms_url
    assert row.quarantined_until is not None


@pytest.mark.asyncio
async def test_delete_sms_config_rejects_active_non_twilio_phone(
    sqlite_engine: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SMSRepository(async_sessionmaker(sqlite_engine, expire_on_commit=False))
    async with repository.Session() as session:
        session.add(
            OrganizationSMSConfigModel(
                sms_config_id="smsc_managed",
                organization_id="o_test",
                mode="connected",
                encrypted_webhook_secret="unused",
                webhook_secret_encrypted_method="aes",
            )
        )
        session.add(
            OrganizationPhoneNumberModel(
                phone_number_id="pn_managed",
                organization_id="o_test",
                sms_config_id="smsc_managed",
                phone_number="+14155550123",
                provider="skyvern",
                status="active",
            )
        )
        await session.commit()
    monkeypatch.setattr(twilio_integration.app, "DATABASE", SimpleNamespace(sms=repository))
    with pytest.raises(HTTPException) as exc_info:
        await twilio_integration.delete_sms_config("smsc_managed", ORG)
    assert exc_info.value.status_code == 409
    async with repository.Session() as session:
        row = await session.scalar(
            select(OrganizationSMSConfigModel).where(OrganizationSMSConfigModel.sms_config_id == "smsc_managed")
        )
    assert row is not None
    assert row.deleted_at is None


@pytest.mark.asyncio
async def test_delete_sms_config_rejects_managed_config(monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database()
    database.sms.get_sms_config.return_value = _config("managed")
    monkeypatch.setattr(twilio_integration.app, "DATABASE", database)
    with pytest.raises(HTTPException) as exc_info:
        await twilio_integration.delete_sms_config("smsc_test", ORG)
    assert exc_info.value.status_code == 409
    database.sms.delete_sms_config.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_detail"),
    [
        ("replace", "Disable all connected Twilio phone numbers before changing Twilio accounts"),
        ("delete", "Disable all connected Twilio phone numbers before disconnecting Twilio"),
    ],
)
async def test_credential_lifecycle_rejects_active_numbers(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    expected_detail: str,
) -> None:
    database = _database(token=_credential_token())
    database.sms.get_connected_sms_config.return_value = (_config(), "webhook-secret")
    database.sms.count_active_twilio_phone_numbers_for_config.return_value = 1
    monkeypatch.setattr(twilio_integration.app, "DATABASE", database)
    _, factory = _patch_client(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        if operation == "replace":
            await twilio_integration.create_twilio_credential(
                CreateTwilioCredentialRequest(
                    credential=TwilioCredential(
                        account_sid=OTHER_ACCOUNT_SID,
                        auth_token="replacement-auth-token",
                    )
                ),
                ORG,
            )
        else:
            await twilio_integration.delete_twilio_credential(ORG)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == expected_detail
    if operation == "replace":
        factory.assert_not_called()
        database.organizations.replace_org_auth_token.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phone_status", ["active", "quarantined"])
async def test_credential_replacement_keeps_signing_token(
    agent_db: AgentDB,
    monkeypatch: pytest.MonkeyPatch,
    phone_status: str,
) -> None:
    database = _database(token=_credential_token())
    database.sms.get_connected_sms_config.return_value = (_config(), "webhook-secret")
    database.sms.count_active_twilio_phone_numbers_for_config = (
        agent_db.sms.count_active_twilio_phone_numbers_for_config
    )
    async with agent_db.Session() as session:
        session.add(OrganizationPhoneNumberModel(**_registered_phone(_config(), status=phone_status).model_dump()))
        await session.commit()
    monkeypatch.setattr(twilio_integration.app, "DATABASE", database)
    _, factory = _patch_client(monkeypatch)
    credential = TwilioCredential(
        account_sid=_credential().account_sid,
        api_key_sid=API_KEY_SID,
        api_key_secret="replacement-api-key-secret",
    )
    with pytest.raises(HTTPException) as caught:
        await twilio_integration.create_twilio_credential(CreateTwilioCredentialRequest(credential=credential), ORG)
    assert caught.value.status_code == 400
    assert "auth token is required" in caught.value.detail
    factory.assert_not_called()
    database.organizations.replace_org_auth_token.assert_not_awaited()
    credential.auth_token = _credential().auth_token
    response = await twilio_integration.create_twilio_credential(
        CreateTwilioCredentialRequest(credential=credential), ORG
    )
    assert response.credential.has_auth_token
    assert database.organizations.replace_org_auth_token.await_args.kwargs["token"].auth_token == credential.auth_token


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["replace", "delete"])
async def test_org_lock_blocks_credential_lifecycle_during_enable(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    database = _database(token=_credential_token())
    config = _config()
    database.sms.get_connected_sms_config.return_value = (config, "webhook-secret")
    saved = SimpleNamespace(phone_number_id="pn_created", status="active")
    database.sms.create_phone_number.return_value = saved
    database.sms.update_phone_number.return_value = saved
    monkeypatch.setattr(twilio_integration.app, "DATABASE", database)
    client, _ = _patch_client(monkeypatch)
    provider = _provider_number(phone_number="+14155550123")
    provider_reads = 0
    enable_locked = asyncio.Event()
    release_enable = asyncio.Event()

    async def provider_lookup(phone_number_sid: str) -> dict[str, object]:
        nonlocal provider_reads
        provider_reads += 1
        if provider_reads == 2:
            enable_locked.set()
            await release_enable.wait()
        return provider

    client.get_incoming_phone_number.side_effect = provider_lookup
    lock = asyncio.Lock()
    lifecycle_waiting = asyncio.Event()

    @asynccontextmanager
    async def lifecycle_lock(organization_id: str, phone_number: str) -> Iterator[None]:
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()

    @asynccontextmanager
    async def organization_lock(organization_id: str) -> Iterator[None]:
        lifecycle_waiting.set()
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()

    database.sms.lifecycle_lock.side_effect = lifecycle_lock
    database.sms.organization_lock.side_effect = organization_lock
    enable_task = asyncio.create_task(
        twilio_integration.enable_twilio_phone_number(
            EnablePhoneNumberRequest(phone_number_sid="PN0123456789abcdef0123456789abcdef"), ORG
        )
    )
    await enable_locked.wait()
    lifecycle_task = asyncio.create_task(
        twilio_integration.create_twilio_credential(CreateTwilioCredentialRequest(credential=_credential()), ORG)
        if operation == "replace"
        else twilio_integration.delete_twilio_credential(ORG)
    )
    await lifecycle_waiting.wait()
    if operation == "replace":
        assert not database.organizations.replace_org_auth_token.await_count
    else:
        assert not database.organizations.invalidate_org_auth_tokens.await_count
    release_enable.set()
    await asyncio.gather(enable_task, lifecycle_task)
    if operation == "replace":
        database.organizations.replace_org_auth_token.assert_awaited_once()
    else:
        database.organizations.invalidate_org_auth_tokens.assert_awaited_once()


@pytest.mark.asyncio
async def test_enable_rejects_cross_org_credential_id(monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(token=_credential_token())
    database.credentials.get_credential.return_value = None
    monkeypatch.setattr(twilio_integration.app, "DATABASE", database)
    with pytest.raises(HTTPException) as exc_info:
        await twilio_integration.enable_twilio_phone_number(
            EnablePhoneNumberRequest(
                phone_number_sid="PN0123456789abcdef0123456789abcdef", credential_id="cred_foreign"
            ),
            ORG,
        )
    assert exc_info.value.status_code == 400
    database.credentials.get_credential.assert_awaited_once_with(
        "cred_foreign",
        organization_id=ORG.organization_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sms_method", "sms_application_sid"),
    [("GET", ""), ("POST", "AP_customer")],
)
async def test_enable_requires_takeover_for_method_or_application_sid(
    monkeypatch: pytest.MonkeyPatch,
    sms_method: str,
    sms_application_sid: str,
) -> None:
    config = _config()
    database = _database(token=_credential_token())
    database.sms.get_connected_sms_config.return_value = (config, "webhook-secret")
    database.sms.get_phone_number_by_number.return_value = _registered_phone(config, status="quarantined")
    monkeypatch.setattr(twilio_integration.app, "DATABASE", database)
    client, _ = _patch_client(monkeypatch)
    client.get_incoming_phone_number.return_value = _provider_number(
        sms_url=twilio_integration._inbound_url(config.sms_config_id, "webhook-secret"),
        sms_method=sms_method,
        sms_application_sid=sms_application_sid,
    )
    with pytest.raises(HTTPException) as exc_info:
        await twilio_integration.enable_twilio_phone_number(
            EnablePhoneNumberRequest(phone_number_sid="PN0123456789abcdef0123456789abcdef"),
            ORG,
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {
        "current_sms_url": twilio_integration.redact_url_for_display(
            twilio_integration.redact_url_query(twilio_integration._inbound_url(config.sms_config_id, "webhook-secret"))
        ),
        "current_sms_method": sms_method,
        "current_sms_application_sid": sms_application_sid or None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["enable", "disable", "disable_decrypt"])
async def test_provider_apply_then_raise_reconciles_local_state(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    database = _database(token=_credential_token())
    config = _config()
    registered = _registered_phone(config, previous_sms_url="skyvern_enc:aesgcm-v1:fixture")
    if operation == "enable":
        saved = SimpleNamespace(phone_number_id="pn_created", status="active")
        database.sms.get_connected_sms_config.return_value = (config, "webhook-secret")
        database.sms.create_phone_number.return_value = saved
        database.sms.update_phone_number.side_effect = [saved, saved]
    else:
        disabled = registered.model_copy(update={"status": "disabled"})
        if operation == "disable_decrypt":
            database.sms.get_phone_number.side_effect = [
                registered,
                registered,
                registered.model_copy(update={"status": "quarantined"}),
            ]
        else:
            database.sms.get_phone_number.return_value = registered
        database.sms.get_sms_config_with_secret.return_value = (config, "webhook-secret")
        database.sms.update_phone_number.side_effect = [registered, disabled]
    monkeypatch.setattr(twilio_integration.app, "DATABASE", database)
    client, _ = _patch_client(monkeypatch)
    decrypt = AsyncMock(return_value="https://customer.example/sms")
    if operation == "disable_decrypt":
        decrypt.side_effect = ValueError("decrypt failed")
    monkeypatch.setattr(twilio_integration, "decrypt_secret_field_value", decrypt)
    provider = _provider_number(
        phone_number="+14155550123",
        sms_url=""
        if operation == "enable"
        else twilio_integration._inbound_url(config.sms_config_id, "webhook-secret"),
    )
    client.get_incoming_phone_number.return_value = provider
    client.update_inbound_sms_config.side_effect = _apply_provider_routing_then_raise(
        provider, TwilioApiError(500, "provider failed")
    )
    with pytest.raises(ValueError if operation == "disable_decrypt" else HTTPException) as exc_info:
        if operation == "enable":
            await twilio_integration.enable_twilio_phone_number(
                EnablePhoneNumberRequest(phone_number_sid="PN0123456789abcdef0123456789abcdef"),
                ORG,
            )
        else:
            await twilio_integration.disable_twilio_phone_number(
                DisablePhoneNumberRequest(phone_number_id="pn_existing"),
                ORG,
            )
    if operation != "disable_decrypt":
        assert exc_info.value.status_code == 502
    assert database.sms.update_phone_number.await_args_list[-1].kwargs["status"] == (
        "active" if operation in {"enable", "disable_decrypt"} else "disabled"
    )
    if operation == "disable":
        assert provider["sms_url"] == "https://customer.example/sms"


@pytest.mark.asyncio
async def test_enable_rejects_skyvern_managed_row_before_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(token=_credential_token())
    database.sms.get_phone_number_by_number.return_value = SimpleNamespace(
        phone_number_id="pn_managed",
        provider="skyvern",
    )
    monkeypatch.setattr(twilio_integration.app, "DATABASE", database)
    client, _ = _patch_client(monkeypatch)
    client.get_incoming_phone_number.return_value = _provider_number(phone_number="+14155550123")
    with pytest.raises(HTTPException) as exc_info:
        await twilio_integration.enable_twilio_phone_number(
            EnablePhoneNumberRequest(phone_number_sid="PN0123456789abcdef0123456789abcdef"),
            ORG,
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Skyvern-managed numbers must be released from the Skyvern Numbers integration"
    database.sms.update_phone_number.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["enable", "disable"])
async def test_provider_apply_then_cancel_reconciles_before_propagating(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    database = _database(token=_credential_token())
    config = _config()
    registered = _registered_phone(config)
    if operation == "enable":
        staged = registered.model_copy(update={"status": "quarantined"})
        final = SimpleNamespace(**{**registered.__dict__, "status": "active"})
        database.sms.get_connected_sms_config.return_value = (config, "webhook-secret")
        database.sms.get_phone_number_by_number.return_value = registered
        initial_sms_url = ""
    else:
        staged = registered.model_copy(update={"status": "quarantined"})
        final = registered.model_copy(update={"status": "disabled"})
        database.sms.get_phone_number.return_value = registered
        database.sms.get_sms_config_with_secret.return_value = (config, "webhook-secret")
        initial_sms_url = twilio_integration._inbound_url(config.sms_config_id, "webhook-secret")
    database.sms.update_phone_number.side_effect = [staged, final]
    monkeypatch.setattr(twilio_integration.app, "DATABASE", database)
    client, _ = _patch_client(monkeypatch)
    provider = _provider_number(phone_number="+14155550123", sms_url=initial_sms_url)
    client.get_incoming_phone_number.return_value = provider
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    async def apply_provider_routing(*args: object, **kwargs: object) -> None:
        provider_started.set()
        await release_provider.wait()
        provider["sms_url"], provider["sms_method"], provider["sms_application_sid"] = args[1:4]

    client.update_inbound_sms_config.side_effect = apply_provider_routing
    request = (
        EnablePhoneNumberRequest(phone_number_sid="PN0123456789abcdef0123456789abcdef", confirm_takeover=True)
        if operation == "enable"
        else DisablePhoneNumberRequest(phone_number_id="pn_existing")
    )
    task = asyncio.create_task(
        twilio_integration.enable_twilio_phone_number(request, ORG)
        if operation == "enable"
        else twilio_integration.disable_twilio_phone_number(request, ORG)
    )
    await provider_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_provider.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider["sms_url"] == (
        twilio_integration._inbound_url(config.sms_config_id, "webhook-secret")
        if operation == "enable"
        else "https://customer.example/sms"
    )


_ORGANIZATION_ID = "org_sms"
_SMS_CONFIG_ID = "smsc_test"
_WEBHOOK_SECRET = "webhook-secret"
_PHONE_NUMBER = "+15555550123"
_MESSAGE_SID = "SM123"
_TWILIO_AUTH_TOKEN = "twilio-auth-token"
_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def _inbound_config(*, daily_ingest_cap: int = 100, mode: str = "connected") -> SimpleNamespace:
    return SimpleNamespace(
        sms_config_id=_SMS_CONFIG_ID,
        organization_id=_ORGANIZATION_ID,
        mode=mode,
        daily_ingest_cap=daily_ingest_cap,
    )


def _inbound_phone_number(*, status: str = "active", sms_config_id: str = _SMS_CONFIG_ID) -> SimpleNamespace:
    return SimpleNamespace(organization_id=_ORGANIZATION_ID, sms_config_id=sms_config_id, status=status)


def _inbound_database(
    *,
    mode: str = "manual",
    phone_number: SimpleNamespace | None = None,
    twilio_token: SimpleNamespace | None = None,
    daily_count: int = 0,
    create_otp_code_if_new: AsyncMock | None = None,
    create_raw_otp_code_if_new: AsyncMock | None = None,
) -> SimpleNamespace:
    database = _database(token=twilio_token)
    database.sms.get_sms_config_with_secret_for_inbound.return_value = (_inbound_config(mode=mode), _WEBHOOK_SECRET)
    database.sms.get_phone_number_by_number.return_value = phone_number or _inbound_phone_number()
    database.otp = SimpleNamespace(
        count_otp_codes_since=AsyncMock(return_value=daily_count),
        create_otp_code_if_new=create_otp_code_if_new
        or AsyncMock(return_value=SimpleNamespace(totp_code_id="otp_short")),
        create_raw_otp_code_if_new=create_raw_otp_code_if_new
        or AsyncMock(return_value=SimpleNamespace(totp_code_id="otp_raw")),
        promote_raw_otp_code=AsyncMock(),
    )
    database.sms.get_sms_config_signing_token.return_value = None
    return database


def _inbound_form(*, body: str = "Your verification code is 123456") -> dict[str, str]:
    return {"From": "+15555550999", "To": _PHONE_NUMBER, "Body": body, "MessageSid": _MESSAGE_SID}


def _post_inbound(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    database: SimpleNamespace,
    *,
    body: str = "Your verification code is 123456",
    config_id: str = _SMS_CONFIG_ID,
    token: str = _WEBHOOK_SECRET,
    form: dict[str, str] | None = None,
    signature: str | None = None,
) -> object:
    monkeypatch.setattr(sms_inbound.app, "DATABASE", database)
    headers = {"X-Twilio-Signature": signature} if signature is not None else {}
    return client.post(
        f"/v1/sms/inbound/{config_id}?token={token}",
        data=_inbound_form(body=body) if form is None else form,
        headers=headers,
    )


def _assert_empty_twiml(response: object) -> None:
    assert response.status_code == 200
    assert response.text == _TWIML
    assert response.headers["content-type"].startswith("text/xml")


@pytest.fixture
def client() -> Iterator[TestClient]:
    fastapi_app = FastAPI()
    fastapi_app.include_router(sms_inbound.sms_inbound_router, prefix="/v1/sms")
    with TestClient(fastapi_app) as test_client:
        yield test_client


@pytest.mark.parametrize("base_url", ["", "   "])
def test_connected_inbound_requires_configured_base_url(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    database = _inbound_database(
        mode="connected",
        twilio_token=SimpleNamespace(
            credential=TwilioCredential(account_sid=ACCOUNT_SID, auth_token=_TWILIO_AUTH_TOKEN)
        ),
    )
    monkeypatch.setattr(sms_inbound.settings, "SKYVERN_BASE_URL", base_url)
    log = Mock()
    verify = Mock(return_value=True)
    monkeypatch.setattr(sms_inbound, "LOG", log)
    monkeypatch.setattr(sms_inbound, "validate_twilio_signature", verify)
    response = _post_inbound(client, monkeypatch, database)
    assert response.status_code == 503
    log.error.assert_called_once()
    verify.assert_not_called()
    database.sms.get_phone_number_by_number.assert_not_awaited()
    print(f"F2-2: unset base URL -> HTTP {response.status_code}; signature verifier not called; one error log")


@pytest.fixture
def twilio_client() -> Iterator[TestClient]:
    api = FastAPI()
    api.include_router(twilio_integration.twilio_integration_router, prefix="/twilio")
    api.dependency_overrides[twilio_integration.org_auth_service.get_current_org_for_credential_routes] = lambda: ORG
    with TestClient(api) as test_client:
        yield test_client


@pytest.mark.parametrize("token_removed_under_lock", [False, True])
def test_enable_requires_auth_token_for_signed_delivery(
    twilio_client: TestClient, monkeypatch: pytest.MonkeyPatch, token_removed_under_lock: bool
) -> None:
    credential = TwilioCredential(
        account_sid=_credential().account_sid,
        api_key_sid=API_KEY_SID,
        api_key_secret="api-key-secret",
    )
    database = _database(token=SimpleNamespace(credential=credential))
    if token_removed_under_lock:
        database.organizations.get_valid_org_auth_token.side_effect = [
            _credential_token(),
            SimpleNamespace(credential=credential),
        ]
    monkeypatch.setattr(twilio_integration.app, "DATABASE", database)
    provider, factory = _patch_client(monkeypatch)
    provider.get_incoming_phone_number.return_value = _provider_number()
    response = twilio_client.post("/twilio/phone-numbers/enable", json={"phone_number_sid": _provider_number()["sid"]})
    assert response.status_code == 400
    assert "auth token is required for signed inbound delivery" in response.json()["detail"]
    database.sms.create_sms_config.assert_not_awaited()
    database.sms.create_phone_number.assert_not_awaited()
    provider.update_inbound_sms_config.assert_not_awaited()
    if not token_removed_under_lock:
        factory.assert_not_called()


@pytest.mark.parametrize("display_url", [None, "https://[invalid?token=prior-secret"])
def test_phone_number_http_responses_exclude_url_secrets(
    twilio_client: TestClient, monkeypatch: pytest.MonkeyPatch, display_url: str | None
) -> None:
    config = _config()
    prior_url = "https://customer.example/sms?token=prior-secret"
    registered = _registered_phone(config, previous_sms_url=prior_url)
    database = _database(token=_credential_token())
    database.sms.get_connected_sms_config.return_value = (config, _WEBHOOK_SECRET)
    database.sms.get_sms_config_with_secret.return_value = (config, _WEBHOOK_SECRET)
    database.sms.get_phone_number_by_number.return_value = registered
    database.sms.get_phone_number.return_value = registered
    database.sms.update_phone_number.return_value = registered
    database.sms.list_phone_numbers.return_value = [registered]
    monkeypatch.setattr(twilio_integration.app, "DATABASE", database)
    monkeypatch.setattr(twilio_integration.settings, "SKYVERN_BASE_URL", "https://api.example.test")
    provider, _ = _patch_client(monkeypatch)
    provider_number = _provider_number(sms_url=prior_url)
    provider.get_incoming_phone_number.return_value = provider_number
    provider.list_incoming_phone_numbers.return_value = [provider_number]

    def assert_safe(response: object, operation: str) -> None:
        assert response.status_code == 200
        assert "token=" not in response.text
        assert "prior-secret" not in response.text
        assert _WEBHOOK_SECRET not in response.text
        assert "previous_sms_url" not in response.text
        print(f"F2-1: {operation} HTTP 200; no token= or URL secrets in response")

    assert_safe(
        twilio_client.post(
            "/twilio/phone-numbers/enable",
            json={"phone_number_sid": provider_number["sid"], "confirm_takeover": True},
        ),
        "enable",
    )
    provider_number["sms_url"] = build_inbound_sms_url(
        "https://api.example.test", config.sms_config_id, _WEBHOOK_SECRET
    )
    if display_url is not None:
        provider.list_incoming_phone_numbers.return_value = [_provider_number(sms_url=display_url)]
    listed = twilio_client.get("/twilio/phone-numbers")
    assert_safe(listed, "list")
    assert "?" not in listed.json()[0]["current_sms_url"]
    assert_safe(twilio_client.get("/twilio/phone-number-registry"), "registry")
    assert_safe(
        twilio_client.post("/twilio/phone-numbers/disable", json={"phone_number_id": registered.phone_number_id}),
        "disable",
    )
    database.sms.get_sms_config_with_secret.assert_awaited_once_with(
        organization_id=ORG.organization_id, sms_config_id=config.sms_config_id
    )
    provider.update_inbound_sms_config.assert_awaited_with(
        registered.provider_number_sid, prior_url, "GET", "AP_customer"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phone_status", ["active", "releasing"])
async def test_valid_ingest_promotes_stored_raw_row_and_returns_xml(
    agent_db: AgentDB,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    phone_status: str,
) -> None:
    base_url = "https://api.example.test"
    body = "Your verification code is 123456"
    form = _inbound_form(body=body)
    database = _inbound_database(
        mode="connected",
        phone_number=_inbound_phone_number(status=phone_status),
        twilio_token=SimpleNamespace(
            credential=TwilioCredential(account_sid=ACCOUNT_SID, auth_token=_TWILIO_AUTH_TOKEN)
        ),
    )
    database.otp = agent_db.otp
    raw_ids: list[str] = []

    async def extract(**kwargs: Any) -> dict[str, object]:
        async with agent_db.Session() as session:
            row = (await session.scalars(select(TOTPCodeModel))).one()
            assert (row.parse_status, row.code, row.content) == ("raw", None, body)
            assert (row.organization_id, row.totp_identifier, row.external_message_id) == (
                _ORGANIZATION_ID,
                _PHONE_NUMBER,
                _MESSAGE_SID,
            )
            raw_ids.append(row.totp_code_id)
        return dict(reasoning="code found", otp_type="totp", otp_value_found=True, otp_value="123456")

    monkeypatch.setattr(otp_service, "get_org_aware_secondary_llm_api_handler", lambda **_: extract)
    monkeypatch.setattr(otp_service.app, "AGENT_FUNCTION", AgentFunction())
    monkeypatch.setattr(sms_inbound.settings, "SKYVERN_BASE_URL", base_url)
    signature = compute_twilio_signature(
        _TWILIO_AUTH_TOKEN,
        build_inbound_sms_url(base_url, _SMS_CONFIG_ID, _WEBHOOK_SECRET),
        form,
    )
    response = _post_inbound(client, monkeypatch, database, form=form, signature=signature)
    _assert_empty_twiml(response)
    async with agent_db.Session() as session:
        row = (await session.scalars(select(TOTPCodeModel))).one()
        assert raw_ids == [row.totp_code_id]
        assert (row.parse_status, row.code, row.otp_type) == ("parsed", "123456", "totp")
    invalid_response = _post_inbound(client, monkeypatch, database, form=form, signature="invalid")
    assert invalid_response.status_code == 400


def test_managed_config_fails_closed_until_persisted_signing_contract(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _inbound_database(
        mode="managed",
    )
    response = _post_inbound(client, monkeypatch, database, body="654321")
    assert response.status_code == 403
    database.organizations.get_valid_org_auth_token.assert_not_awaited()


@pytest.mark.parametrize(
    "twilio_token",
    [
        None,
        SimpleNamespace(
            credential=TwilioCredential(
                account_sid=ACCOUNT_SID,
                api_key_sid=API_KEY_SID,
                api_key_secret="api-key-secret",
            )
        ),
        SimpleNamespace(
            credential=TwilioCredential(
                account_sid=ACCOUNT_SID,
                api_key_sid=API_KEY_SID,
                api_key_secret="api-key-secret",
                auth_token=None,
            )
        ),
        SimpleNamespace(credential=None),
        SimpleNamespace(credential=SimpleNamespace()),
        SimpleNamespace(credential=SimpleNamespace(account_sid=ACCOUNT_SID, auth_token=None)),
    ],
    ids=[
        "missing-auth-token",
        "typed-credential-without-auth-token",
        "typed-credential-with-explicit-null-auth-token",
        "null-credential",
        "missing-auth-token-attribute",
        "credential-without-auth-token",
    ],
)
def test_connected_config_fails_closed_without_auth_token(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    twilio_token: SimpleNamespace | None,
) -> None:
    database = _inbound_database(
        mode="connected",
        twilio_token=twilio_token,
    )
    response = _post_inbound(client, monkeypatch, database)

    assert response.status_code == 403
    database.organizations.get_valid_org_auth_token.assert_awaited_once()
    database.sms.get_phone_number_by_number.assert_not_awaited()
    database.otp.create_raw_otp_code_if_new.assert_not_awaited()


@pytest.mark.parametrize(
    ("phone_status", "phone_config_id"),
    [(None, _SMS_CONFIG_ID), ("released", _SMS_CONFIG_ID), ("active", "smsc_other")],
)
def test_receiver_drops_unregistered_or_foreign_phone_rows(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    phone_status: str | None,
    phone_config_id: str,
) -> None:
    database = _inbound_database(
        phone_number=_inbound_phone_number(status=phone_status, sms_config_id=phone_config_id)
        if phone_status is not None
        else _inbound_phone_number()
    )
    if phone_status is None:
        database.sms.get_phone_number_by_number.return_value = None
    response = _post_inbound(client, monkeypatch, database)
    _assert_empty_twiml(response)


@pytest.mark.parametrize("raw", [False, True])
def test_duplicate_message_sid_does_not_create_or_schedule_again(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    raw: bool,
) -> None:
    duplicate = AsyncMock(return_value=None)
    parse_and_promote = AsyncMock()
    database = _inbound_database(
        create_otp_code_if_new=None if raw else duplicate,
        create_raw_otp_code_if_new=duplicate if raw else None,
    )
    if raw:
        monkeypatch.setattr(sms_inbound, "_parse_and_promote", parse_and_promote)
    response = _post_inbound(client, monkeypatch, database, body="Your code is 123456" if raw else "123456")
    _assert_empty_twiml(response)
    assert duplicate.await_args.kwargs["external_message_id"] == _MESSAGE_SID
    if raw:
        parse_and_promote.assert_not_awaited()
        database.otp.create_otp_code_if_new.assert_not_awaited()
    else:
        database.otp.create_raw_otp_code_if_new.assert_not_awaited()


TOTP_IDENTIFIER = "(415) 555-2671"
NORMALIZED_TOTP_IDENTIFIER = "+14155552671"


def _make_context() -> WorkflowRunContext:
    return WorkflowRunContext(
        workflow_title="workflow",
        workflow_id="workflow-id",
        workflow_permanent_id="workflow-permanent-id",
        workflow_run_id="workflow-run-id",
        aws_client=SimpleNamespace(),
    )


def _make_vault_parameter(
    kind: str, totp_identifier: str | None
) -> OnePasswordCredentialParameter | BitwardenLoginCredentialParameter:
    now = datetime.now(UTC)
    if kind == "onepassword":
        return OnePasswordCredentialParameter(
            onepassword_credential_parameter_id="opcp-test",
            workflow_id="workflow-id",
            key="onepassword_login",
            vault_id="vault-id",
            item_id="item-id",
            totp_identifier=totp_identifier,
            created_at=now,
            modified_at=now,
        )
    return BitwardenLoginCredentialParameter(
        bitwarden_login_credential_parameter_id="blcp-test",
        workflow_id="workflow-id",
        key="bitwarden_login",
        bitwarden_client_id_aws_secret_key="client-id-secret",
        bitwarden_client_secret_aws_secret_key="client-secret-secret",
        bitwarden_master_password_aws_secret_key="master-password-secret",
        bitwarden_collection_id="collection-id",
        bitwarden_item_id="item-id",
        totp_identifier=totp_identifier,
        created_at=now,
        modified_at=now,
    )


@pytest.mark.parametrize("parameter_type", ["onepassword", "bitwarden"])
def test_vault_yaml_to_model_round_trips_totp_identifier(parameter_type: str) -> None:
    if parameter_type == "onepassword":
        yaml_parameter = OnePasswordCredentialParameterYAML(
            key="onepassword_login",
            vault_id="vault-id",
            item_id="item-id",
            totp_identifier=TOTP_IDENTIFIER,
            totp_field_name="otp_code",
        )
        parameter_cls = OnePasswordCredentialParameter
        model_cls = OnePasswordCredentialParameterModel
    else:
        yaml_parameter = BitwardenLoginCredentialParameterYAML(
            key="bitwarden_login",
            bitwarden_client_id_aws_secret_key="client-id-secret",
            bitwarden_client_secret_aws_secret_key="client-secret",
            bitwarden_master_password_aws_secret_key="master-password-secret",
            bitwarden_collection_id="collection-id",
            bitwarden_item_id="item-id",
            totp_identifier=TOTP_IDENTIFIER,
        )
        parameter_cls = BitwardenLoginCredentialParameter
        model_cls = BitwardenLoginCredentialParameterModel
    parameter = convert_workflow_definition(
        WorkflowDefinitionYAML(parameters=[yaml_parameter], blocks=[]), workflow_id="workflow-id"
    ).parameters[0]
    assert isinstance(parameter, parameter_cls)
    if parameter_type == "onepassword":
        assert parameter.totp_field_name == "otp_code"
    model = WorkflowParametersRepository._convert_parameter_to_model(parameter)
    assert isinstance(model, model_cls)
    assert model.totp_identifier == TOTP_IDENTIFIER
    converted = (
        OnePasswordCredentialParameter.model_validate(model, from_attributes=True)
        if parameter_type == "onepassword"
        else convert_to_bitwarden_login_credential_parameter(model)
    )
    assert converted.totp_identifier == TOTP_IDENTIFIER


@pytest.mark.asyncio
async def test_vault_parameter_repository_commit_normalizes_utc_timestamps() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    models = (BitwardenLoginCredentialParameterModel, OnePasswordCredentialParameterModel)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=[model.__table__ for model in models])
    writes: list[dict[str, datetime]] = []

    def capture_timestamps(_conn: Any, _cursor: Any, _sql: Any, _parameters: Any, context: Any, _many: Any) -> None:
        if context.isinsert or context.isupdate:
            for parameters in context.compiled_parameters:
                writes.append({key: value for key, value in parameters.items() if isinstance(value, datetime)})

    event.listen(engine.sync_engine, "before_cursor_execute", capture_timestamps)
    repository = WorkflowParametersRepository(async_sessionmaker(engine, expire_on_commit=False))
    await repository.save_workflow_definition_parameters(
        [_make_vault_parameter(kind, TOTP_IDENTIFIER) for kind in ("bitwarden", "onepassword")]
    )
    assert len(writes) == 2
    for write in writes:
        assert {"created_at", "modified_at"} <= write.keys()
        assert all(value.tzinfo is None for value in write.values())
    async with repository.Session() as session:
        rows = [await session.scalar(select(model)) for model in models]
    assert all(
        getattr(row, field).tzinfo is None for row in rows if row is not None for field in ("created_at", "modified_at")
    )
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("credential_type", "credential_fields"),
    [
        (
            LoginCredentialType.onepassword,
            {
                "onepassword_vault_id": "vault-id",
                "onepassword_item_id": "item-id",
                "onepassword_totp_field_name": "otp_code",
            },
        ),
        (
            LoginCredentialType.bitwarden,
            {"bitwarden_collection_id": "collection-id", "bitwarden_item_id": "item-id"},
        ),
    ],
)
async def test_login_route_parameters_reach_converter(
    monkeypatch: pytest.MonkeyPatch,
    credential_type: LoginCredentialType,
    credential_fields: dict[str, str],
) -> None:
    now = datetime.now(UTC)
    organization = Organization(
        organization_id="organization-id",
        organization_name="Test organization",
        created_at=now,
        modified_at=now,
    )
    new_workflow = SimpleNamespace(
        title="Login", description=None, status=WorkflowStatus.auto_generated, workflow_permanent_id="wpid_login"
    )
    workflow = SimpleNamespace(workflow_id="workflow-id")
    workflow_service = SimpleNamespace(
        create_empty_workflow=AsyncMock(return_value=new_workflow),
        create_workflow_from_request=AsyncMock(return_value=workflow),
    )
    monkeypatch.setattr(
        run_blocks,
        "app",
        SimpleNamespace(
            WORKFLOW_SERVICE=workflow_service, RATE_LIMITER=SimpleNamespace(rate_limit_submit_run=AsyncMock())
        ),
    )
    run_result = object()
    monkeypatch.setattr(run_blocks, "_run_workflow_and_build_response", AsyncMock(return_value=run_result))
    login_request = LoginRequest(
        url="https://example.com/login",
        credential_type=credential_type,
        totp_identifier=TOTP_IDENTIFIER,
        **credential_fields,
    )
    caller = SimpleNamespace(organization=organization, caller_type=CallerType.API_KEY)
    result = await run_blocks.login(
        request=SimpleNamespace(), background_tasks=SimpleNamespace(), login_request=login_request, caller=caller
    )
    assert result is run_result
    yaml_request = workflow_service.create_workflow_from_request.await_args.kwargs["request"]
    converted = convert_workflow_definition(yaml_request.workflow_definition, workflow_id="workflow-id")
    parameter = next(parameter for parameter in converted.parameters if parameter.key == "credential")
    assert parameter.totp_identifier == TOTP_IDENTIFIER
    if credential_type is LoginCredentialType.onepassword:
        assert isinstance(parameter, OnePasswordCredentialParameter)
        assert parameter.vault_id == "vault-id"
        assert parameter.item_id == "item-id"
        assert parameter.totp_field_name == "otp_code"
    else:
        assert isinstance(parameter, BitwardenLoginCredentialParameter)
        assert parameter.bitwarden_collection_id == "collection-id"
        assert parameter.bitwarden_item_id == "item-id"
        assert parameter.url_parameter_key == "https://example.com/login"


async def _register(
    context: WorkflowRunContext,
    kind: str,
    parameter: OnePasswordCredentialParameter | BitwardenLoginCredentialParameter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if kind == "onepassword":
        organizations = SimpleNamespace(
            get_valid_org_auth_token=AsyncMock(return_value=SimpleNamespace(token="service-account-token"))
        )
        monkeypatch.setattr(cm, "app", SimpleNamespace(DATABASE=SimpleNamespace(organizations=organizations)))
        item = SimpleNamespace(fields=[], notes=None)
        client = SimpleNamespace(items=SimpleNamespace(get=AsyncMock(return_value=item)))
        monkeypatch.setattr(cm, "OnePasswordClient", SimpleNamespace(authenticate=AsyncMock(return_value=client)))
        await context.register_onepassword_credential_parameter_value(
            parameter, SimpleNamespace(organization_id="organization-id")
        )
        return
    secret_credentials = {
        BitwardenConstants.USERNAME: "username",
        BitwardenConstants.PASSWORD: "password",
        BitwardenConstants.TOTP: "",
    }
    context._run_bitwarden_operation_with_fallback = AsyncMock(
        return_value=(secret_credentials, ("client-id", "client-secret", "master-password", None))
    )
    await context.register_bitwarden_login_credential_parameter_value(
        parameter,
        SimpleNamespace(
            organization_id="organization-id",
            bw_organization_id="bitwarden-organization-id",
            bw_collection_ids=["collection-id"],
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("totp_identifier", "expected"),
    [
        (
            TOTP_IDENTIFIER,
            {"onepassword_login": NORMALIZED_TOTP_IDENTIFIER, "bitwarden_login": NORMALIZED_TOTP_IDENTIFIER},
        ),
        (
            "otp1234567890@example.com",
            {"onepassword_login": "otp1234567890@example.com", "bitwarden_login": "otp1234567890@example.com"},
        ),
        (
            "login-1234567890",
            {"onepassword_login": "login-1234567890", "bitwarden_login": "login-1234567890"},
        ),
        (None, {}),
    ],
)
async def test_vault_parameters_register_totp_identifiers(
    monkeypatch: pytest.MonkeyPatch,
    totp_identifier: str | None,
    expected: dict[str, str],
) -> None:
    context = _make_context()
    await _register(context, "onepassword", _make_vault_parameter("onepassword", totp_identifier), monkeypatch)
    await _register(context, "bitwarden", _make_vault_parameter("bitwarden", totp_identifier), monkeypatch)
    assert context.credential_totp_identifiers == expected


def test_password_response_exposes_seed_presence_without_secret() -> None:
    credential = SimpleNamespace(
        credential_id="cred_seed",
        credential_type=CredentialType.PASSWORD,
        username="user@example.test",
        totp_type=TotpType.AUTHENTICATOR,
        totp_identifier="+14155550123",
        has_totp_seed=True,
        name="Login",
        vault_type=CredentialVaultType.SKYVERN,
        browser_profile_id=None,
        auto_profile_disabled=False,
        pin_saved_session_ip=False,
        tested_url=None,
        user_context=None,
        save_browser_session_intent=False,
        run_sequentially=False,
        folder_id=None,
        proxy_location=None,
        proxy_session_id=None,
        created_by=None,
    )
    response = credentials._convert_to_response(credential).model_dump()

    assert response["credential"]["has_totp"] is True
    assert response["credential"]["totp_identifier"] == "+14155550123"
    assert "has_totp_seed" not in response["credential"]
    assert "totp" not in response["credential"]


@pytest.mark.asyncio
async def test_seed_presence_survives_update_when_totp_is_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    update_credential = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(
        credentials.app,
        "DATABASE",
        SimpleNamespace(credentials=SimpleNamespace(update_credential_vault_data=update_credential)),
    )
    existing = SimpleNamespace(credential_id="cred-seed", organization_id="o_test")
    request = CreateCredentialRequest(
        name="Login",
        credential_type=CredentialType.PASSWORD,
        credential=NonEmptyPasswordCredential(username="user@example.test", password="new-password"),
    )

    await credentials.CredentialVaultService._update_db_credential(existing, request, "item-new")

    assert "has_totp_seed" not in update_credential.await_args.kwargs


class TestCredentialVaultLegacySeedPreservation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("has_totp_seed", [None, True])
    async def test_local_vault_replacement_item_preserves_omitted_totp(self, has_totp_seed: bool | None) -> None:
        service = SkyvernCredentialVaultService()
        existing = SimpleNamespace(credential_id="cred-seed", item_id="item-old", organization_id="org-test")
        existing.has_totp_seed = has_totp_seed
        existing_item = CredentialItem(
            item_id="item-old",
            name="Login",
            credential_type=CredentialType.PASSWORD,
            credential=PasswordCredential(username="old-user", password="old-password", totp="JBSWY3DPEHPK3PXP"),
        )
        stored = AsyncMock()
        service.get_credential_item = AsyncMock(return_value=existing_item)
        service._store_item = stored
        service._update_db_credential = AsyncMock(return_value=SimpleNamespace(item_id="item-new"))
        service._generate_item_id = lambda: "item-new"
        request = CreateCredentialRequest(
            name="Login",
            credential_type=CredentialType.PASSWORD,
            credential=NonEmptyPasswordCredential(username="new-user", password="new-password", metadata={"k": "v"}),
        )

        await service.update_credential(existing, request)

        replacement = stored.await_args.args[0]
        assert replacement.item_id == "item-new"
        assert replacement.credential.totp == "JBSWY3DPEHPK3PXP"
        assert service._update_db_credential.await_args.kwargs["data"].credential.totp == replacement.credential.totp
        assert "totp" in service._update_db_credential.await_args.kwargs["data"].credential.model_fields_set
        service.get_credential_item.assert_awaited_once_with(existing)

    @pytest.mark.asyncio
    async def test_local_vault_known_absent_totp_skips_read(self) -> None:
        service = SkyvernCredentialVaultService()
        existing = SimpleNamespace(
            credential_id="cred-seed", item_id="item-old", organization_id="org-test", has_totp_seed=False
        )
        service.get_credential_item = AsyncMock()
        service._store_item = AsyncMock()
        service._update_db_credential = AsyncMock(return_value=SimpleNamespace(item_id="item-new"))
        service._generate_item_id = lambda: "item-new"
        request = CreateCredentialRequest(
            name="Login",
            credential_type=CredentialType.PASSWORD,
            credential=NonEmptyPasswordCredential(username="new-user", password="new-password", metadata={"k": "v"}),
        )

        await service.update_credential(existing, request)

        service.get_credential_item.assert_not_awaited()
        assert service._update_db_credential.await_args.kwargs["data"] is request
        assert "totp" not in service._update_db_credential.await_args.kwargs["data"].credential.model_fields_set


@pytest.mark.parametrize("valid", [True, False])
def test_managed_inbound_signing_token(client: TestClient, monkeypatch: pytest.MonkeyPatch, valid: bool) -> None:
    database = _inbound_database(mode="managed")
    database.sms.get_sms_config_signing_token.return_value = "managed-secret"
    base_url = "https://api.example.test"
    monkeypatch.setattr(sms_inbound.settings, "SKYVERN_BASE_URL", base_url)
    form = _inbound_form(body="123456")
    signature = compute_twilio_signature(
        "managed-secret", build_inbound_sms_url(base_url, _SMS_CONFIG_ID, _WEBHOOK_SECRET), form
    )
    response = _post_inbound(client, monkeypatch, database, form=form, signature=signature if valid else "bad")
    assert response.status_code == (200 if valid else 400)
    assert database.otp.create_otp_code_if_new.await_count == int(valid)


@pytest.mark.parametrize("invalid_on_read", [False, True])
def test_unknown_inbound_mode_fails_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, invalid_on_read: bool
) -> None:
    database = _inbound_database(mode="unknown")
    if invalid_on_read:
        with pytest.raises(ValidationError) as error:
            _config("unknown")
        database.sms.get_sms_config_with_secret_for_inbound.side_effect = error.value
    log = Mock()
    monkeypatch.setattr(sms_inbound, "LOG", log)
    response = _post_inbound(client, monkeypatch, database)
    assert response.status_code == 403
    log.error.assert_called_once()
    database.otp.create_otp_code_if_new.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["permit", "checkout", "advisory"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_postgres_lock_acquisition_deadline_and_cancellation(monkeypatch, stage, cancel):
    from contextlib import asynccontextmanager

    from skyvern.forge.sdk.db.repositories import sms as module

    monkeypatch.setattr(module, "LIFECYCLE_LOCK_TIMEOUT_SECONDS", 0.05)
    waiting = asyncio.Event()
    calls = []

    async def wait_forever():
        waiting.set()
        await asyncio.Event().wait()

    async def scalar(statement, params):
        calls.append(params["lock_key"])
        if len(calls) == 1:
            return True
        await wait_forever()

    session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        connection=AsyncMock(side_effect=wait_forever if stage == "checkout" else None),
        scalar=AsyncMock(side_effect=scalar),
        invalidate=AsyncMock(),
    )

    @asynccontextmanager
    async def session_factory():
        yield session

    @asynccontextmanager
    async def permit():
        if stage == "permit":
            await wait_forever()
        yield

    repository = SMSRepository(session_factory)
    monkeypatch.setattr(repository, "_lifecycle_permit", permit)

    async def acquire():
        async with repository.lifecycle_lock("org", "+14155552671"):
            pytest.fail("acquired contended resource")

    task = asyncio.create_task(acquire())
    await waiting.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else module.SMSLifecycleLockTimeout):
        await task
    assert session.invalidate.await_count == int(stage != "permit")
    if stage == "advisory":
        assert len(calls) == 2


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/phone-numbers/enable", {"phone_number_sid": "PN0123456789abcdef0123456789abcdef"}),
        ("POST", "/phone-numbers/disable", {"phone_number_id": "pn_existing"}),
        (
            "POST",
            "/credentials",
            {"credential": {"account_sid": ACCOUNT_SID, "auth_token": "token"}},
        ),
        ("DELETE", "/credentials", None),
        ("POST", "/sms-configs", {"mode": "manual"}),
        ("DELETE", "/sms-configs/smsc_test", None),
        ("POST", "/sms-configs/smsc_test/phone-numbers", {"phone_number": "+14155550123"}),
    ],
)
def test_lifecycle_lock_timeout_returns_conflict_on_every_mutation(
    twilio_client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str, payload: dict | None
) -> None:
    from skyvern.forge.sdk.db.repositories.sms import SMSLifecycleLockTimeout

    database = _database(token=_credential_token())
    database.sms.get_phone_number.return_value = _registered_phone(_config())
    database.sms.get_sms_config.return_value = _config(mode="manual")
    provider, _ = _patch_client(monkeypatch)
    provider.get_incoming_phone_number.return_value = _provider_number()

    @asynccontextmanager
    async def timed_out(*args):
        raise SMSLifecycleLockTimeout("busy")
        yield

    database.sms.lifecycle_lock.side_effect = timed_out
    database.sms.organization_lock.side_effect = timed_out
    monkeypatch.setattr(twilio_integration.app, "DATABASE", database)
    response = twilio_client.request(method, f"/twilio{path}", json=payload)
    assert response.status_code == 409
    assert response.json()["detail"] == "SMS number operation is busy. Try again."
    provider.update_inbound_sms_config.assert_not_awaited()
