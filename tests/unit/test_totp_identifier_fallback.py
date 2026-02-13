from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.schemas.credentials import CredentialVaultType
from skyvern.forge.sdk.workflow import context_manager as cm
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import TaskV2Block


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

    class FakeDatabase:
        async def get_credential(self, credential_id: str, organization_id: str) -> object:
            assert credential_id == "cred-1"
            assert organization_id == "org-1"
            return db_credential

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


def test_task_v2_block_resolves_totp_identifier_from_context() -> None:
    block = TaskV2Block.model_construct(totp_identifier=None)
    workflow_run_context = SimpleNamespace(credential_totp_identifiers={"credential_param": "user@example.com"})

    assert block._resolve_totp_identifier(workflow_run_context) == "user@example.com"

    block_with_explicit_totp = TaskV2Block.model_construct(totp_identifier="provided@example.com")
    assert block_with_explicit_totp._resolve_totp_identifier(workflow_run_context) == "provided@example.com"
