from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.custom_llm_registry import (
    custom_llm_key,
    custom_llm_model_name,
    deregister_custom_llm_config,
    register_custom_llm_config,
)
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.routes import custom_llms as routes
from skyvern.forge.sdk.schemas.custom_llms import (
    CUSTOM_LLM_API_KEY_MASK,
    CustomLLMConfig,
    CustomLLMCreateRequest,
    CustomLLMUpdateRequest,
)
from skyvern.forge.sdk.schemas.organizations import Organization, OrganizationAuthToken
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, TaskV2Status
from skyvern.services import task_v2_service


class FakeOrganizationsRepository:
    def __init__(self) -> None:
        self.tokens: list[OrganizationAuthToken] = []
        self.next_id = 1

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

    async def create_org_auth_token(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
        token: str,
    ) -> OrganizationAuthToken:
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
    ) -> OrganizationAuthToken:
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


def _org() -> Organization:
    now = datetime.now(timezone.utc)
    return Organization(organization_id="o_test", organization_name="Test Org", created_at=now, modified_at=now)


@pytest.fixture
def fake_organizations(monkeypatch: pytest.MonkeyPatch) -> FakeOrganizationsRepository:
    organizations = FakeOrganizationsRepository()
    monkeypatch.setattr(routes.app, "DATABASE", SimpleNamespace(organizations=organizations))
    monkeypatch.setattr(routes.settings, "ENV", "local")
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
    assert LLMConfigRegistry.is_registered(llm_key)
    registered_config = LLMConfigRegistry.get_config(llm_key)
    assert registered_config.model_name == "openrouter/anthropic/claude-3.5-sonnet"
    assert registered_config.litellm_params
    assert registered_config.litellm_params["api_key"] == "sk-or"
    assert create_response.custom_llm.config.api_key == CUSTOM_LLM_API_KEY_MASK
    assert custom_llm_model_name(custom_llm_id) in routes.settings.get_model_name_to_llm_key()

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

    assert update_response.custom_llm.id == custom_llm_id
    assert LLMConfigRegistry.get_config(llm_key).model_name == "ollama_chat/llama3.1"

    delete_response = await routes.delete_custom_llm(custom_llm_id, org)
    assert delete_response.success is True
    assert not LLMConfigRegistry.is_registered(llm_key)
    assert fake_organizations.tokens[0].valid is False


@pytest.mark.asyncio
async def test_list_custom_llms_does_not_register_configs(
    fake_organizations: FakeOrganizationsRepository,
) -> None:
    org = _org()
    token = await fake_organizations.create_org_auth_token(
        organization_id=org.organization_id,
        token_type=OrganizationAuthTokenType.custom_llm,
        token=CustomLLMConfig(
            display_name="Local Llama",
            provider="ollama",
            model_name="llama3.1",
        ).model_dump_json(),
    )
    llm_key = custom_llm_key(token.id)
    deregister_custom_llm_config(token.id)

    list_response = await routes.list_custom_llms(org)

    assert [custom_llm.id for custom_llm in list_response.custom_llms] == [token.id]
    assert not LLMConfigRegistry.is_registered(llm_key)


@pytest.mark.asyncio
async def test_update_custom_llm_preserves_masked_api_key(
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

        mapping = routes.settings.get_model_name_to_llm_key()
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


@pytest.mark.asyncio
async def test_task_v2_metadata_uses_selected_custom_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    org = _org()
    custom_llm_id = "oat_custom_metadata"
    register_config = CustomLLMConfig(
        display_name="Metadata Ollama",
        provider="ollama",
        model_name="llama3.1",
    )
    register_custom_llm_config(custom_llm_id, register_config)
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
