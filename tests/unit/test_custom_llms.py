import socket
from collections.abc import Generator
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.config import settings
from skyvern.exceptions import BlockedHost
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.custom_llm_registry import (
    custom_llm_key,
    custom_llm_model_name,
    deregister_custom_llm_config,
    get_custom_llm_model_mappings,
    register_custom_llm_config,
)
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.routes import agent_protocol
from skyvern.forge.sdk.routes import custom_llms as routes
from skyvern.forge.sdk.schemas.custom_llms import (
    CUSTOM_LLM_API_KEY_MASK,
    CustomLLMConfig,
    CustomLLMCreateRequest,
    CustomLLMUpdateRequest,
)
from skyvern.forge.sdk.schemas.organizations import Organization, OrganizationAuthToken
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, TaskV2Status
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.services import task_v1_service, task_v2_service


class FakeOrganizationsRepository:
    def __init__(self) -> None:
        self.tokens: list[OrganizationAuthToken] = []
        self.next_id = 1
        self.created_encrypted_methods: list[EncryptMethod | None] = []
        self.updated_encrypted_methods: list[EncryptMethod | None] = []

    async def get_valid_org_auth_tokens(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
    ) -> list[OrganizationAuthToken]:
        return [
            token
            for token in self.tokens
            if token.organization_id == organization_id and token.token_type == token_type and token.valid
        ]

    async def get_valid_org_auth_tokens_by_type(
        self,
        token_type: OrganizationAuthTokenType,
    ) -> list[OrganizationAuthToken]:
        return [token for token in self.tokens if token.token_type == token_type and token.valid]

    async def create_org_auth_token(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
        token: str,
        encrypted_method: EncryptMethod | None = None,
    ) -> OrganizationAuthToken:
        self.created_encrypted_methods.append(encrypted_method)
        now = datetime.now(timezone.utc)
        auth_token = OrganizationAuthToken(
            id=f"oat_custom_{self.next_id}",
            organization_id=organization_id,
            token_type=token_type,
            token=token,
            valid=True,
            created_at=now,
            modified_at=now,
        )
        self.next_id += 1
        self.tokens.append(auth_token)
        return auth_token

    async def update_org_auth_token(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
        token_id: str,
        token: str,
        encrypted_method: EncryptMethod | None = None,
    ) -> OrganizationAuthToken:
        self.updated_encrypted_methods.append(encrypted_method)
        for auth_token in self.tokens:
            if (
                auth_token.id == token_id
                and auth_token.organization_id == organization_id
                and auth_token.token_type == token_type
                and auth_token.valid
            ):
                auth_token.token = token
                auth_token.modified_at = datetime.now(timezone.utc)
                return auth_token
        raise NotFoundError("Organization auth token not found")

    async def invalidate_org_auth_token(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
        token_id: str,
    ) -> None:
        for auth_token in self.tokens:
            if (
                auth_token.id == token_id
                and auth_token.organization_id == organization_id
                and auth_token.token_type == token_type
                and auth_token.valid
            ):
                auth_token.valid = False
                return
        raise NotFoundError("Organization auth token not found")


def _org(organization_id: str = "o_test") -> Organization:
    now = datetime.now(timezone.utc)
    return Organization(organization_id=organization_id, organization_name="Test Org", created_at=now, modified_at=now)


@pytest.fixture(autouse=True)
def base_settings_manager() -> Generator[None, None, None]:
    previous_settings = SettingsManager.get_settings()
    SettingsManager.set_settings(settings)
    yield
    SettingsManager.set_settings(previous_settings)


@pytest.fixture
def fake_organizations(monkeypatch: pytest.MonkeyPatch) -> FakeOrganizationsRepository:
    organizations = FakeOrganizationsRepository()
    fake_database = SimpleNamespace(organizations=organizations)
    monkeypatch.setattr(routes.app, "DATABASE", fake_database)
    monkeypatch.setattr(agent_protocol.app, "DATABASE", fake_database)
    monkeypatch.setattr(task_v1_service.app, "DATABASE", fake_database)
    monkeypatch.setattr(task_v2_service.app, "DATABASE", fake_database)
    return organizations


@pytest.mark.asyncio
async def test_custom_llm_routes_register_update_and_delete_config(
    fake_organizations: FakeOrganizationsRepository,
) -> None:
    org = _org()
    create_response = await routes.create_custom_llm(
        CustomLLMCreateRequest(
            config=CustomLLMConfig(
                display_name="OpenRouter Claude",
                provider="openrouter",
                model_name="anthropic/claude-3.5-sonnet",
                api_key="sk-or",
            )
        ),
        org,
    )

    custom_llm_id = create_response.custom_llm.id
    llm_key = custom_llm_key(custom_llm_id)
    assert fake_organizations.created_encrypted_methods == [None]
    assert LLMConfigRegistry.is_registered(llm_key)
    registered_config = LLMConfigRegistry.get_config(llm_key)
    assert registered_config.model_name == "openrouter/anthropic/claude-3.5-sonnet"
    assert registered_config.litellm_params
    assert registered_config.litellm_params["api_key"] == "sk-or"
    assert create_response.custom_llm.config.api_key == CUSTOM_LLM_API_KEY_MASK
    assert custom_llm_model_name(custom_llm_id) in settings.get_model_name_to_llm_key(
        organization_id=org.organization_id
    )

    list_response = await routes.list_custom_llms(org)
    assert [custom_llm.id for custom_llm in list_response.custom_llms] == [custom_llm_id]
    assert list_response.custom_llms[0].config.api_key == CUSTOM_LLM_API_KEY_MASK

    update_response = await routes.update_custom_llm(
        CustomLLMUpdateRequest(
            config=CustomLLMConfig(
                display_name="Local Llama",
                provider="ollama",
                model_name="llama3.1",
            )
        ),
        custom_llm_id,
        org,
    )

    assert fake_organizations.updated_encrypted_methods == [None]
    assert update_response.custom_llm.id == custom_llm_id
    assert LLMConfigRegistry.get_config(llm_key).model_name == "ollama_chat/llama3.1"

    delete_response = await routes.delete_custom_llm(custom_llm_id, org)
    assert delete_response.success is True
    assert not LLMConfigRegistry.is_registered(llm_key)
    assert fake_organizations.tokens[0].valid is False


@pytest.mark.asyncio
async def test_update_custom_llm_preserves_masked_api_key(
    monkeypatch: pytest.MonkeyPatch,
    fake_organizations: FakeOrganizationsRepository,
) -> None:
    validate_api_base = AsyncMock()
    monkeypatch.setattr(routes, "_validate_custom_llm_api_base", validate_api_base)
    org = _org()
    create_response = await routes.create_custom_llm(
        CustomLLMCreateRequest(
            config=CustomLLMConfig(
                display_name="OpenRouter Claude",
                provider="openrouter",
                model_name="anthropic/claude-3.5-sonnet",
                api_key="sk-or",
            )
        ),
        org,
    )
    custom_llm_id = create_response.custom_llm.id

    update_response = await routes.update_custom_llm(
        CustomLLMUpdateRequest(
            config=CustomLLMConfig(
                display_name="OpenRouter GPT",
                provider="openrouter",
                model_name="openai/gpt-4.1",
                api_key=CUSTOM_LLM_API_KEY_MASK,
            )
        ),
        custom_llm_id,
        org,
    )

    assert update_response.custom_llm.config.api_key == CUSTOM_LLM_API_KEY_MASK
    stored_config = CustomLLMConfig.model_validate_json(fake_organizations.tokens[0].token)
    assert stored_config.api_key == "sk-or"
    registered_config = LLMConfigRegistry.get_config(custom_llm_key(custom_llm_id))
    assert registered_config.litellm_params
    assert registered_config.litellm_params["api_key"] == "sk-or"
    assert validate_api_base.await_count == 2

    deregister_custom_llm_config(custom_llm_id)


@pytest.mark.asyncio
async def test_models_route_lists_only_current_org_custom_llms_with_unique_labels(
    fake_organizations: FakeOrganizationsRepository,
) -> None:
    org = _org("o_models_1")
    other_org = _org("o_models_2")
    custom_llm_ids: set[str] = set()

    try:
        first_response = await routes.create_custom_llm(
            CustomLLMCreateRequest(
                config=CustomLLMConfig(
                    display_name="Local Llama",
                    provider="ollama",
                    model_name="llama3.1",
                )
            ),
            org,
        )
        second_response = await routes.create_custom_llm(
            CustomLLMCreateRequest(
                config=CustomLLMConfig(
                    display_name="Local Llama",
                    provider="ollama",
                    model_name="mistral",
                )
            ),
            org,
        )
        other_response = await routes.create_custom_llm(
            CustomLLMCreateRequest(
                config=CustomLLMConfig(
                    display_name="Other Org Llama",
                    provider="ollama",
                    model_name="llama3.1",
                )
            ),
            other_org,
        )
        custom_llm_ids = {
            first_response.custom_llm.id,
            second_response.custom_llm.id,
            other_response.custom_llm.id,
        }

        response = await agent_protocol.models(org)

        first_model_name = custom_llm_model_name(first_response.custom_llm.id)
        second_model_name = custom_llm_model_name(second_response.custom_llm.id)
        other_model_name = custom_llm_model_name(other_response.custom_llm.id)
        assert first_model_name in response.models
        assert second_model_name in response.models
        assert other_model_name not in response.models
        first_label = response.models[first_model_name]
        second_label = response.models[second_model_name]
        assert first_response.custom_llm.id in first_label
        assert second_response.custom_llm.id in second_label
        assert first_label != second_label
    finally:
        for custom_llm_id in custom_llm_ids:
            deregister_custom_llm_config(custom_llm_id)


@pytest.mark.asyncio
async def test_custom_llm_routes_allow_multiple_registered_configs(
    fake_organizations: FakeOrganizationsRepository,
) -> None:
    org = _org()
    custom_llm_ids: set[str] = set()

    try:
        ollama_response = await routes.create_custom_llm(
            CustomLLMCreateRequest(
                config=CustomLLMConfig(
                    display_name="Local Llama",
                    provider="ollama",
                    model_name="llama3.1",
                )
            ),
            org,
        )
        openrouter_response = await routes.create_custom_llm(
            CustomLLMCreateRequest(
                config=CustomLLMConfig(
                    display_name="OpenRouter Claude",
                    provider="openrouter",
                    model_name="anthropic/claude-3.5-sonnet",
                    api_key="sk-or",
                )
            ),
            org,
        )

        custom_llm_ids = {ollama_response.custom_llm.id, openrouter_response.custom_llm.id}
        list_response = await routes.list_custom_llms(org)
        assert {custom_llm.id for custom_llm in list_response.custom_llms} == custom_llm_ids

        mapping = settings.get_model_name_to_llm_key(organization_id=org.organization_id)
        for custom_llm_id in custom_llm_ids:
            llm_key = custom_llm_key(custom_llm_id)
            assert LLMConfigRegistry.is_registered(llm_key)
            assert custom_llm_model_name(custom_llm_id) in mapping
            assert mapping[custom_llm_model_name(custom_llm_id)]["llm_key"] == llm_key

        assert LLMConfigRegistry.get_config(custom_llm_key(ollama_response.custom_llm.id)).model_name == (
            "ollama_chat/llama3.1"
        )
        assert LLMConfigRegistry.get_config(custom_llm_key(openrouter_response.custom_llm.id)).model_name == (
            "openrouter/anthropic/claude-3.5-sonnet"
        )
    finally:
        for custom_llm_id in custom_llm_ids:
            deregister_custom_llm_config(custom_llm_id)


def test_custom_llm_model_mappings_require_organization_scope() -> None:
    org_custom_llm_id = "oat_custom_mapping_org"
    other_custom_llm_id = "oat_custom_mapping_other"
    register_custom_llm_config(
        org_custom_llm_id,
        "o_mapping_org",
        CustomLLMConfig(
            display_name="Org Llama",
            provider="ollama",
            model_name="llama3.1",
        ),
    )
    register_custom_llm_config(
        other_custom_llm_id,
        "o_mapping_other",
        CustomLLMConfig(
            display_name="Other Llama",
            provider="ollama",
            model_name="mistral",
        ),
    )

    try:
        assert get_custom_llm_model_mappings() == {}
        org_mapping = get_custom_llm_model_mappings("o_mapping_org")
        assert custom_llm_model_name(org_custom_llm_id) in org_mapping
        assert custom_llm_model_name(other_custom_llm_id) not in org_mapping
    finally:
        deregister_custom_llm_config(org_custom_llm_id)
        deregister_custom_llm_config(other_custom_llm_id)


def test_cloud_custom_llm_api_base_blocks_local_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SettingsManager.get_settings(), "ALLOW_CUSTOM_LLM_LOCAL_API_BASES", False, raising=False)

    with pytest.raises(ValueError, match="blocked"):
        CustomLLMConfig(
            display_name="Cloud Ollama",
            provider="ollama",
            model_name="llama3.1",
        )


@pytest.mark.asyncio
async def test_cloud_custom_llm_create_blocks_private_dns_answer_before_write(
    monkeypatch: pytest.MonkeyPatch,
    fake_organizations: FakeOrganizationsRepository,
) -> None:
    monkeypatch.setattr(SettingsManager.get_settings(), "ALLOW_CUSTOM_LLM_LOCAL_API_BASES", False, raising=False)
    resolver = MagicMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("172.16.0.42", 443))])
    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolver)

    request = CustomLLMCreateRequest(
        config=CustomLLMConfig(
            display_name="Cloud endpoint",
            provider="openai_compatible",
            model_name="example-model",
            api_base="https://llm.example.test/v1",
            api_key="test-key",
        )
    )
    resolver.assert_not_called()

    with pytest.raises(BlockedHost):
        await routes.create_custom_llm(request, _org())

    assert fake_organizations.tokens == []


def test_stored_custom_llm_validation_allows_legacy_api_base_without_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SettingsManager.get_settings(), "ALLOW_CUSTOM_LLM_LOCAL_API_BASES", False, raising=False)
    resolver = MagicMock(side_effect=AssertionError("stored custom LLM reads must not resolve DNS"))
    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolver)

    CustomLLMConfig.model_validate_json(
        '{"display_name":"Stored endpoint","provider":"openrouter","model_name":"example/model",'
        '"api_base":"https://gateway.example.test/v1","api_key":"test-key"}'
    )

    resolver.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "api_base", "error"),
    [
        ("openai_compatible", "http://llm.example.test/v1", "Cloud api_base must use HTTPS on port 443"),
        ("openai_compatible", "https://llm.example.test:8443/v1", "Cloud api_base must use HTTPS on port 443"),
        ("openrouter", "https://gateway.example.test/v1", "OpenRouter api_base must use openrouter.ai"),
    ],
)
async def test_cloud_custom_llm_api_base_restrictions_apply_only_at_write_boundary(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    api_base: str,
    error: str,
) -> None:
    monkeypatch.setattr(SettingsManager.get_settings(), "ALLOW_CUSTOM_LLM_LOCAL_API_BASES", False, raising=False)
    config = CustomLLMConfig(
        display_name="Cloud endpoint",
        provider=provider,  # type: ignore[arg-type]
        model_name="example/model",
        api_base=api_base,
        api_key="test-key",
    )
    with pytest.raises(routes.HTTPException, match=error):
        await routes._validate_custom_llm_api_base(config)


def test_custom_llm_api_base_allows_local_targets_for_self_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SettingsManager.get_settings(), "ALLOW_CUSTOM_LLM_LOCAL_API_BASES", True, raising=False)

    config = CustomLLMConfig(
        display_name="Local Ollama",
        provider="ollama",
        model_name="llama3.1",
    )

    assert config.api_base == "http://localhost:11434"


@pytest.mark.asyncio
async def test_task_v2_metadata_uses_selected_custom_llm(
    monkeypatch: pytest.MonkeyPatch,
    fake_organizations: FakeOrganizationsRepository,
) -> None:
    monkeypatch.setattr(
        "skyvern.utils.url_validators.socket.getaddrinfo",
        lambda host, port, *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0))],
    )
    org = _org()
    custom_llm_id = "oat_custom_metadata"
    register_custom_llm_config(
        custom_llm_id,
        org.organization_id,
        CustomLLMConfig(
            display_name="Metadata Ollama",
            provider="ollama",
            model_name="llama3.1",
        ),
    )
    now = datetime.now(timezone.utc)
    task_v2 = TaskV2(
        task_id="tsk_v2_custom",
        status=TaskV2Status.created,
        organization_id=org.organization_id,
        workflow_run_id="wr_custom",
        workflow_id="wf_custom",
        workflow_permanent_id="wpid_custom",
        prompt="Use the selected model",
        url=None,
        model={"model_name": custom_llm_model_name(custom_llm_id)},
        created_at=now,
        modified_at=now,
    )
    workflow = SimpleNamespace(workflow_id="wf_custom", workflow_permanent_id="wpid_custom")
    workflow_run = SimpleNamespace(workflow_run_id="wr_custom")
    thought = SimpleNamespace(observer_thought_id="ot_custom")
    observer = SimpleNamespace(
        create_thought=AsyncMock(return_value=thought),
        update_thought=AsyncMock(),
        update_task_v2=AsyncMock(return_value=task_v2),
    )
    fake_db = SimpleNamespace(
        organizations=fake_organizations,
        observer=observer,
        workflows=SimpleNamespace(update_workflow=AsyncMock()),
        tasks=SimpleNamespace(get_run=AsyncMock(return_value=None), update_task_run=AsyncMock()),
    )
    default_handler = AsyncMock(side_effect=AssertionError("default LLM handler should not be used"))
    custom_handler = AsyncMock(
        return_value={
            "url": "https://example.com",
            "title": "Custom metadata",
            "thoughts": "Used selected custom model",
        }
    )

    def fake_get_override_llm_api_handler(override_llm_key: str | None, *, default: object) -> object:
        assert override_llm_key == custom_llm_key(custom_llm_id)
        assert default is default_handler
        return custom_handler

    monkeypatch.setattr(task_v2_service.app, "DATABASE", fake_db)
    monkeypatch.setattr(task_v2_service.app, "LLM_API_HANDLER", default_handler)
    monkeypatch.setattr(
        task_v2_service.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        fake_get_override_llm_api_handler,
    )

    try:
        await task_v2_service.initialize_task_v2_metadata(
            organization=org,
            task_v2=task_v2,
            workflow=workflow,
            workflow_run=workflow_run,
            user_prompt="Use the selected model",
            current_browser_url=None,
            user_url="https://example.com",
        )
    finally:
        deregister_custom_llm_config(custom_llm_id)

    custom_handler.assert_awaited_once()
    default_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_v2_validation_registers_custom_llm_on_demand(
    fake_organizations: FakeOrganizationsRepository,
) -> None:
    org = _org()
    token = await fake_organizations.create_org_auth_token(
        organization_id=org.organization_id,
        token_type=OrganizationAuthTokenType.custom_llm,
        token=CustomLLMConfig(
            display_name="On Demand Ollama",
            provider="ollama",
            model_name="llama3.1",
        ).model_dump_json(),
    )
    deregister_custom_llm_config(token.id)

    await task_v2_service._validate_task_v2_model_for_org(org, {"model_name": custom_llm_model_name(token.id)})

    assert LLMConfigRegistry.is_registered(custom_llm_key(token.id))
    deregister_custom_llm_config(token.id)


@pytest.mark.asyncio
async def test_task_v1_validation_registers_custom_llm_on_demand(
    fake_organizations: FakeOrganizationsRepository,
) -> None:
    org = _org()
    token = await fake_organizations.create_org_auth_token(
        organization_id=org.organization_id,
        token_type=OrganizationAuthTokenType.custom_llm,
        token=CustomLLMConfig(
            display_name="Task V1 Ollama",
            provider="ollama",
            model_name="llama3.1",
        ).model_dump_json(),
    )
    deregister_custom_llm_config(token.id)

    await task_v1_service._validate_task_v1_model_for_org(org, {"model_name": custom_llm_model_name(token.id)})

    assert LLMConfigRegistry.is_registered(custom_llm_key(token.id))
    deregister_custom_llm_config(token.id)


@pytest.mark.asyncio
async def test_task_v1_rejects_custom_llm_from_another_org(
    fake_organizations: FakeOrganizationsRepository,
) -> None:
    owner_org = _org("o_task_v1_owner")
    requester_org = _org("o_task_v1_requester")
    token = await fake_organizations.create_org_auth_token(
        organization_id=owner_org.organization_id,
        token_type=OrganizationAuthTokenType.custom_llm,
        token=CustomLLMConfig(
            display_name="Task V1 Owner Llama",
            provider="ollama",
            model_name="llama3.1",
        ).model_dump_json(),
    )

    with pytest.raises(task_v1_service.InvalidTaskV1ModelError):
        await task_v1_service._validate_task_v1_model_for_org(
            requester_org,
            {"model_name": custom_llm_model_name(token.id)},
        )


@pytest.mark.asyncio
async def test_task_v2_rejects_custom_llm_from_another_org(
    fake_organizations: FakeOrganizationsRepository,
) -> None:
    owner_org = _org("o_owner")
    requester_org = _org("o_requester")
    token = await fake_organizations.create_org_auth_token(
        organization_id=owner_org.organization_id,
        token_type=OrganizationAuthTokenType.custom_llm,
        token=CustomLLMConfig(
            display_name="Owner Llama",
            provider="ollama",
            model_name="llama3.1",
        ).model_dump_json(),
    )

    with pytest.raises(task_v2_service.InvalidTaskV2ModelError):
        await task_v2_service._validate_task_v2_model_for_org(
            requester_org,
            {"model_name": custom_llm_model_name(token.id)},
        )


def test_task_v2_selected_non_custom_model_override_is_intentional(monkeypatch: pytest.MonkeyPatch) -> None:
    org = _org()
    now = datetime.now(timezone.utc)
    task_v2 = TaskV2(
        task_id="tsk_v2_non_custom",
        status=TaskV2Status.created,
        organization_id=org.organization_id,
        workflow_run_id="wr_non_custom",
        workflow_id="wf_non_custom",
        workflow_permanent_id="wpid_non_custom",
        prompt="Use the selected non-custom model",
        url=None,
        model={"model_name": "gemini-2.5-flash"},
        created_at=now,
        modified_at=now,
    )
    default_handler = object()
    selected_handler = object()

    def fake_get_override_llm_api_handler(override_llm_key: str | None, *, default: object) -> object:
        assert override_llm_key == task_v2.llm_key
        assert override_llm_key is not None
        assert default is default_handler
        return selected_handler

    monkeypatch.setattr(task_v2_service.app, "LLM_API_HANDLER", default_handler)
    monkeypatch.setattr(
        task_v2_service.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        fake_get_override_llm_api_handler,
    )

    assert task_v2_service._get_task_v2_llm_api_handler(task_v2) is selected_handler


def test_custom_ollama_chat_models_skip_max_token_parameters() -> None:
    custom_llm_id = "oat_custom_ollama_params"
    register_custom_llm_config(
        custom_llm_id,
        "o_test",
        CustomLLMConfig(
            display_name="Ollama Params",
            provider="ollama",
            model_name="llama3.1",
            max_completion_tokens=1024,
            temperature=0.1,
        ),
    )

    try:
        llm_config = LLMConfigRegistry.get_config(custom_llm_key(custom_llm_id))
        params = LLMAPIHandlerFactory.get_api_parameters(llm_config)
    finally:
        deregister_custom_llm_config(custom_llm_id)

    assert "max_completion_tokens" not in params
    assert "max_tokens" not in params
    assert params["temperature"] == 0.1
