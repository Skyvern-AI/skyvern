from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from skyvern.exceptions import BitwardenListItemsError
from skyvern.forge.sdk.workflow import context_manager as cm
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.parameter import (
    BitwardenCreditCardDataParameter,
    BitwardenLoginCredentialParameter,
    BitwardenSensitiveInformationParameter,
)


@pytest.mark.asyncio
async def test_org_email_bitwarden_auth_falls_back_to_global_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_token = SimpleNamespace(
        credential=SimpleNamespace(
            email="test@example.com",
            master_password="org-master-password",
        )
    )

    class FakeOrganizationsRepo:
        async def get_valid_org_auth_token(self, organization_id: str, token_type: str) -> object:
            assert organization_id == "org-1"
            assert token_type == "bitwarden_credential"
            return org_token

    class FakeDatabase:
        def __init__(self) -> None:
            self.organizations = FakeOrganizationsRepo()

    fake_app = SimpleNamespace(DATABASE=FakeDatabase())
    monkeypatch.setattr(cm, "app", fake_app)

    fake_settings = SimpleNamespace(
        BITWARDEN_CLIENT_ID="global-client-id",
        BITWARDEN_CLIENT_SECRET="global-client-secret",
        BITWARDEN_MASTER_PASSWORD="global-master-password",
        BITWARDEN_EMAIL=None,
    )
    monkeypatch.setattr(cm, "settings", fake_settings)

    calls: list[dict[str, object]] = []

    async def fake_get_secret_value_from_url(
        client_id: str | None,
        client_secret: str | None,
        master_password: str,
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
        url: str | None = None,
        collection_id: str | None = None,
        item_id: str | None = None,
        max_retries: int = 2,
        timeout: int = 60,
        email: str | None = None,
    ) -> dict[str, str]:
        calls.append(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "master_password": master_password,
                "email": email,
                "url": url,
                "collection_id": collection_id,
                "item_id": item_id,
            }
        )
        if len(calls) == 1:
            raise BitwardenListItemsError(
                "Bitwarden CLI failed after all retry attempts. Fail reasons: ['TimeoutError: ']"
            )
        return {
            "BW_USERNAME": "fallback-user",
            "BW_PASSWORD": "fallback-password",
            "BW_TOTP": "",
        }

    monkeypatch.setattr(cm.BitwardenService, "get_secret_value_from_url", fake_get_secret_value_from_url)

    context = WorkflowRunContext(
        workflow_title="title",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_run_id="wr-1",
        aws_client=SimpleNamespace(),
    )
    context.values["target_url"] = "https://www.example.com/login"

    parameter = BitwardenLoginCredentialParameter(
        key="bitwarden_login",
        description="Bitwarden login",
        bitwarden_login_credential_parameter_id="blc_1",
        workflow_id="wf-1",
        bitwarden_client_id_aws_secret_key="unused-client-id-secret",
        bitwarden_client_secret_aws_secret_key="unused-client-secret",
        bitwarden_master_password_aws_secret_key="unused-master-password",
        url_parameter_key="target_url",
        bitwarden_collection_id=None,
        bitwarden_item_id=None,
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
    )

    organization = SimpleNamespace(
        organization_id="org-1",
        bw_organization_id="bw-org-1",
        bw_collection_ids=["col-1"],
    )

    await context.register_bitwarden_login_credential_parameter_value(parameter, organization)

    assert len(calls) == 2
    assert calls[0]["email"] == "test@example.com"
    assert calls[0]["client_id"] is None
    assert calls[0]["client_secret"] is None
    assert calls[0]["master_password"] == "org-master-password"

    assert calls[1]["email"] is None
    assert calls[1]["client_id"] == "global-client-id"
    assert calls[1]["client_secret"] == "global-client-secret"
    assert calls[1]["master_password"] == "global-master-password"

    stored = context.values["bitwarden_login"]
    assert stored["username"].endswith("_username")
    assert stored["password"].endswith("_password")


@pytest.mark.asyncio
async def test_bitwarden_login_error_includes_resolved_collection_and_item_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOrganizationsRepo:
        async def get_valid_org_auth_token(self, organization_id: str, token_type: str) -> object | None:
            assert organization_id == "org-1"
            assert token_type == "bitwarden_credential"
            return None

    class FakeDatabase:
        def __init__(self) -> None:
            self.organizations = FakeOrganizationsRepo()

    monkeypatch.setattr(cm, "app", SimpleNamespace(DATABASE=FakeDatabase()))
    monkeypatch.setattr(
        cm,
        "settings",
        SimpleNamespace(
            BITWARDEN_CLIENT_ID="global-client-id",
            BITWARDEN_CLIENT_SECRET="global-client-secret",
            BITWARDEN_MASTER_PASSWORD="global-master-password",
            BITWARDEN_EMAIL=None,
        ),
    )

    async def fake_get_secret_value_from_url(
        client_id: str | None,
        client_secret: str | None,
        master_password: str,
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
        url: str | None = None,
        collection_id: str | None = None,
        item_id: str | None = None,
        max_retries: int = 2,
        timeout: int = 60,
        email: str | None = None,
    ) -> dict[str, str]:
        assert collection_id == "resolved-collection-id"
        assert item_id == "resolved-item-id"
        raise BitwardenListItemsError("lookup failed")

    monkeypatch.setattr(cm.BitwardenService, "get_secret_value_from_url", fake_get_secret_value_from_url)

    context = WorkflowRunContext(
        workflow_title="title",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_run_id="wr-1",
        aws_client=SimpleNamespace(),
    )
    context.values["target_url"] = "https://www.example.com/login"
    context.values["collection_param"] = "resolved-collection-id"
    context.values["item_param"] = "resolved-item-id"

    parameter = BitwardenLoginCredentialParameter(
        key="bitwarden_login",
        description="Bitwarden login",
        bitwarden_login_credential_parameter_id="blc_1",
        workflow_id="wf-1",
        bitwarden_client_id_aws_secret_key="unused-client-id-secret",
        bitwarden_client_secret_aws_secret_key="unused-client-secret",
        bitwarden_master_password_aws_secret_key="unused-master-password",
        url_parameter_key="target_url",
        bitwarden_collection_id="{{ collection_param }}",
        bitwarden_item_id="{{ item_param }}",
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
    )
    organization = SimpleNamespace(
        organization_id="org-1",
        bw_organization_id="bw-org-1",
        bw_collection_ids=["resolved-collection-id"],
    )

    with pytest.raises(BitwardenListItemsError) as exc_info:
        await context.register_bitwarden_login_credential_parameter_value(parameter, organization)

    message = exc_info.value.message
    assert "collection_id=resolved-collection-id" in message
    assert "item_id=resolved-item-id" in message
    assert "{{ collection_param }}" not in message
    assert "{{ item_param }}" not in message
    assert "global-client-secret" not in message
    assert "global-master-password" not in message


@pytest.mark.asyncio
async def test_bitwarden_sensitive_information_resolves_collection_and_identity_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOrganizationsRepo:
        async def get_valid_org_auth_token(self, organization_id: str, token_type: str) -> object | None:
            assert organization_id == "org-1"
            assert token_type == "bitwarden_credential"
            return None

    class FakeDatabase:
        def __init__(self) -> None:
            self.organizations = FakeOrganizationsRepo()

    monkeypatch.setattr(cm, "app", SimpleNamespace(DATABASE=FakeDatabase()))
    monkeypatch.setattr(
        cm,
        "settings",
        SimpleNamespace(
            BITWARDEN_CLIENT_ID="global-client-id",
            BITWARDEN_CLIENT_SECRET="global-client-secret",
            BITWARDEN_MASTER_PASSWORD="global-master-password",
            BITWARDEN_EMAIL=None,
        ),
    )

    async def fake_get_sensitive_information_from_identity(
        client_id: str | None,
        client_secret: str | None,
        master_password: str,
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
        collection_id: str,
        identity_key: str,
        identity_fields: list[str],
        remaining_retries: int = 2,
        timeout: int = 60,
        fail_reasons: list[str] | None = None,
        email: str | None = None,
    ) -> dict[str, str]:
        assert collection_id == "resolved-collection-id"
        assert identity_key == "resolved-identity-key"
        assert identity_fields == ["ssn"]
        raise BitwardenListItemsError("lookup failed")

    monkeypatch.setattr(
        cm.BitwardenService,
        "get_sensitive_information_from_identity",
        fake_get_sensitive_information_from_identity,
    )

    context = WorkflowRunContext(
        workflow_title="title",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_run_id="wr-1",
        aws_client=SimpleNamespace(),
    )
    context.values["collection_param"] = "resolved-collection-id"
    context.values["identity_param"] = "resolved-identity-key"

    parameter = BitwardenSensitiveInformationParameter(
        key="bitwarden_identity",
        description="Bitwarden identity",
        bitwarden_sensitive_information_parameter_id="bsi_1",
        workflow_id="wf-1",
        bitwarden_client_id_aws_secret_key="unused-client-id-secret",
        bitwarden_client_secret_aws_secret_key="unused-client-secret",
        bitwarden_master_password_aws_secret_key="unused-master-password",
        bitwarden_collection_id="{{ collection_param }}",
        bitwarden_identity_key="{{ identity_param }}",
        bitwarden_identity_fields=["ssn"],
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
    )
    organization = SimpleNamespace(
        organization_id="org-1",
        bw_organization_id="bw-org-1",
        bw_collection_ids=["resolved-collection-id"],
    )

    with pytest.raises(BitwardenListItemsError) as exc_info:
        await context.register_bitwarden_sensitive_information_parameter_value(parameter, organization)

    message = exc_info.value.message
    assert "collection_id=resolved-collection-id" in message
    assert "{{ collection_param }}" not in message
    assert "{{ identity_param }}" not in message
    assert "global-client-secret" not in message
    assert "global-master-password" not in message


@pytest.mark.asyncio
async def test_bitwarden_credit_card_resolves_collection_and_item_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeOrganizationsRepo:
        async def get_valid_org_auth_token(self, organization_id: str, token_type: str) -> object | None:
            assert organization_id == "org-1"
            assert token_type == "bitwarden_credential"
            return None

    class FakeDatabase:
        def __init__(self) -> None:
            self.organizations = FakeOrganizationsRepo()

    monkeypatch.setattr(cm, "app", SimpleNamespace(DATABASE=FakeDatabase()))
    monkeypatch.setattr(
        cm,
        "settings",
        SimpleNamespace(
            BITWARDEN_CLIENT_ID="global-client-id",
            BITWARDEN_CLIENT_SECRET="global-client-secret",
            BITWARDEN_MASTER_PASSWORD="global-master-password",
            BITWARDEN_EMAIL=None,
        ),
    )

    async def fake_get_credit_card_data(
        client_id: str | None,
        client_secret: str | None,
        master_password: str,
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
        collection_id: str,
        item_id: str,
        remaining_retries: int = 2,
        fail_reasons: list[str] | None = None,
        email: str | None = None,
    ) -> dict[str, str]:
        assert collection_id == "resolved-collection-id"
        assert item_id == "resolved-item-id"
        raise BitwardenListItemsError("lookup failed")

    monkeypatch.setattr(cm.BitwardenService, "get_credit_card_data", fake_get_credit_card_data)

    context = WorkflowRunContext(
        workflow_title="title",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_run_id="wr-1",
        aws_client=SimpleNamespace(),
    )
    context.values["collection_param"] = "resolved-collection-id"
    context.values["item_param"] = "resolved-item-id"

    parameter = BitwardenCreditCardDataParameter(
        key="bitwarden_card",
        description="Bitwarden credit card",
        bitwarden_credit_card_data_parameter_id="bcc_1",
        workflow_id="wf-1",
        bitwarden_client_id_aws_secret_key="unused-client-id-secret",
        bitwarden_client_secret_aws_secret_key="unused-client-secret",
        bitwarden_master_password_aws_secret_key="unused-master-password",
        bitwarden_collection_id="{{ collection_param }}",
        bitwarden_item_id="{{ item_param }}",
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
    )
    organization = SimpleNamespace(
        organization_id="org-1",
        bw_organization_id="bw-org-1",
        bw_collection_ids=["resolved-collection-id"],
    )

    with pytest.raises(BitwardenListItemsError) as exc_info:
        await context.register_bitwarden_credit_card_data_parameter_value(parameter, organization)

    message = exc_info.value.message
    assert "collection_id=resolved-collection-id" in message
    assert "item_id=resolved-item-id" in message
    assert "{{ collection_param }}" not in message
    assert "{{ item_param }}" not in message
    assert "global-client-secret" not in message
    assert "global-master-password" not in message
