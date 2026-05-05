from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.schemas.credentials import CredentialVaultType, PasswordCredential
from skyvern.forge.sdk.workflow import context_manager as cm
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import TaskV2Block
from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter


@pytest.mark.asyncio
async def test_register_credential_parameter_uses_db_totp_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    db_credential = SimpleNamespace(
        credential_id="cred-1",
        organization_id="org-1",
        vault_type=CredentialVaultType.BITWARDEN,
        totp_identifier="user@example.com",
    )

    class FakeCredential:
        def __init__(self) -> None:
            self.totp_identifier = None
            self.totp = None

        def model_dump(self) -> dict:
            return {}

    class FakeCredentialItem:
        def __init__(self) -> None:
            self.credential = FakeCredential()

    class FakeCredentialService:
        async def get_credential_item(self, _db_credential: object) -> FakeCredentialItem:
            return FakeCredentialItem()

    class FakeCredentialRepo:
        async def get_credential(self, credential_id: str, organization_id: str) -> object:
            assert credential_id == "cred-1"
            assert organization_id == "org-1"
            return db_credential

    class FakeDatabase:
        def __init__(self) -> None:
            self.credentials = FakeCredentialRepo()

    fake_app = SimpleNamespace(
        DATABASE=FakeDatabase(),
        CREDENTIAL_VAULT_SERVICES={CredentialVaultType.BITWARDEN: FakeCredentialService()},
    )
    monkeypatch.setattr(cm, "app", fake_app)

    context = WorkflowRunContext(
        workflow_title="title",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_run_id="wr-1",
        aws_client=SimpleNamespace(),
    )

    parameter = SimpleNamespace(key="credential_param")
    organization = SimpleNamespace(organization_id="org-1")

    await context._register_credential_parameter_value("cred-1", parameter, organization)

    assert context.get_credential_totp_identifier("credential_param") == "user@example.com"


async def _register_with_credential(
    monkeypatch: pytest.MonkeyPatch, credential: PasswordCredential
) -> WorkflowRunContext:
    db_credential = SimpleNamespace(
        credential_id="cred-1",
        organization_id="org-1",
        vault_type=CredentialVaultType.BITWARDEN,
        totp_identifier=None,
    )

    class FakeCredentialItem:
        def __init__(self, cred: PasswordCredential) -> None:
            self.credential = cred

    class FakeCredentialService:
        async def get_credential_item(self, _db_credential: object) -> FakeCredentialItem:
            return FakeCredentialItem(credential)

    class FakeCredentialRepo:
        async def get_credential(self, credential_id: str, organization_id: str) -> object:
            return db_credential

    class FakeDatabase:
        def __init__(self) -> None:
            self.credentials = FakeCredentialRepo()

    fake_app = SimpleNamespace(
        DATABASE=FakeDatabase(),
        CREDENTIAL_VAULT_SERVICES={CredentialVaultType.BITWARDEN: FakeCredentialService()},
    )
    monkeypatch.setattr(cm, "app", fake_app)

    context = WorkflowRunContext(
        workflow_title="title",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_run_id="wr-1",
        aws_client=SimpleNamespace(),
    )
    parameter = CredentialParameter.model_construct(
        key="credential_param",
        credential_parameter_id="cp-1",
        workflow_id="wf-1",
        credential_id="cred-1",
    )
    organization = SimpleNamespace(organization_id="org-1")
    await context._register_credential_parameter_value("cred-1", parameter, organization)
    return context


@pytest.mark.asyncio
async def test_register_credential_registers_totp_seed_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = PasswordCredential(
        username="user@example.com",
        password="secret",
        totp="JBSWY3DPEHPK3PXP",
    )
    context = await _register_with_credential(monkeypatch, credential)
    assert "totp" in context.values["credential_param"]
    totp_secret_id = context.values["credential_param"]["totp"]
    totp_seed = context.secrets[context.totp_secret_value_key(totp_secret_id)]
    assert totp_seed == "JBSWY3DPEHPK3PXP"


@pytest.mark.asyncio
async def test_register_credential_skips_totp_when_seed_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = PasswordCredential(
        username="user@example.com",
        password="secret",
        totp=None,
    )
    context = await _register_with_credential(monkeypatch, credential)
    assert "totp" not in context.values["credential_param"]


@pytest.mark.asyncio
async def test_find_credential_parameter_key_for_secret_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = PasswordCredential(
        username="user@example.com",
        password="secret",
        totp="JBSWY3DPEHPK3PXP",
    )
    context = await _register_with_credential(monkeypatch, credential)
    username_secret_id = context.values["credential_param"]["username"]
    assert context.find_credential_parameter_key_for_secret(username_secret_id) == "credential_param"
    assert context.find_credential_parameter_key_for_secret("nonexistent") is None


def test_task_v2_block_resolves_totp_identifier_from_context() -> None:
    block = TaskV2Block.model_construct(totp_identifier=None)
    workflow_run_context = SimpleNamespace(credential_totp_identifiers={"credential_param": "user@example.com"})

    assert block._resolve_totp_identifier(workflow_run_context) == "user@example.com"

    block_with_explicit_totp = TaskV2Block.model_construct(totp_identifier="provided@example.com")
    assert block_with_explicit_totp._resolve_totp_identifier(workflow_run_context) == "provided@example.com"
