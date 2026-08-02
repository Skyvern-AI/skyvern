import runpy
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.api.llm import config_registry
from skyvern.forge.sdk.api.llm.api_handler import dummy_llm_api_handler
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.exceptions import InvalidLLMConfigError, MissingLLMProviderEnvVarsError
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.routes import internal_llms
from skyvern.forge.sdk.routes.internal_llms import LLMDiagnosticsStatus
from skyvern.forge.sdk.schemas.custom_llms import CustomLLMConfig
from skyvern.forge.sdk.schemas.organizations import Organization, OrganizationAuthToken
from skyvern.forge.sdk.services.local_org_auth_token_service import SKYVERN_LOCAL_DOMAIN
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.schemas import llm as llm_schemas
from skyvern.schemas.llm import LLMConfig


class FakeOrganizationsRepository:
    def __init__(self, organization: Organization | None, tokens: list[OrganizationAuthToken] | None = None) -> None:
        self.organization = organization
        self.tokens = tokens or []

    async def get_organization_by_domain(self, domain: str) -> Organization | None:
        if domain == SKYVERN_LOCAL_DOMAIN:
            return self.organization
        return None

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


@pytest.fixture(autouse=True)
def restore_llm_registry() -> Iterator[None]:
    original_settings = SettingsManager.get_settings()
    original_configs = dict(LLMConfigRegistry._configs)
    original_issues = dict(LLMConfigRegistry._config_issues)

    SettingsManager.set_settings(settings)
    LLMConfigRegistry._configs.clear()
    LLMConfigRegistry._config_issues.clear()

    yield

    LLMConfigRegistry._configs.clear()
    LLMConfigRegistry._configs.update(original_configs)
    LLMConfigRegistry._config_issues.clear()
    LLMConfigRegistry._config_issues.update(original_issues)
    SettingsManager.set_settings(original_settings)


def _org(organization_id: str = "o_local") -> Organization:
    now = datetime.now(timezone.utc)
    return Organization(
        organization_id=organization_id,
        organization_name="Skyvern-local",
        domain=SKYVERN_LOCAL_DOMAIN,
        created_at=now,
        modified_at=now,
    )


def _custom_llm_token(organization_id: str) -> OrganizationAuthToken:
    now = datetime.now(timezone.utc)
    config = CustomLLMConfig(display_name="Local Llama", provider="ollama", model_name="llama3.1")
    return OrganizationAuthToken(
        id="oat_custom_1",
        organization_id=organization_id,
        token_type=OrganizationAuthTokenType.custom_llm,
        token=config.model_dump_json(),
        valid=True,
        created_at=now,
        modified_at=now,
    )


def test_local_registry_records_missing_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_registry.settings, "ENV", "local")

    LLMConfigRegistry.register_config(
        "UNIT_MISSING",
        LLMConfig("unit/missing", ["UNIT_MISSING_LLM_API_KEY"], supports_vision=True, add_assistant_prefix=False),
    )

    issue = LLMConfigRegistry.get_config_issue("UNIT_MISSING")
    assert issue is not None
    assert issue.missing_env_vars == ("UNIT_MISSING_LLM_API_KEY",)
    assert not LLMConfigRegistry.is_registered("UNIT_MISSING")

    with pytest.raises(InvalidLLMConfigError):
        LLMConfigRegistry.get_config("UNIT_MISSING")

    assert LLMAPIHandlerFactory.get_llm_api_handler("UNIT_MISSING") is dummy_llm_api_handler


def test_local_registry_propagates_config_issues_to_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_registry.settings, "ENV", "local")

    LLMConfigRegistry.register_config(
        "UNIT_MISSING",
        LLMConfig("unit/missing", ["UNIT_MISSING_LLM_API_KEY"], supports_vision=True, add_assistant_prefix=False),
    )
    LLMConfigRegistry.register_config_alias("UNIT_MISSING_ALIAS", "UNIT_MISSING")

    issue = LLMConfigRegistry.get_config_issue("UNIT_MISSING_ALIAS")
    assert issue is not None
    assert issue.missing_env_vars == ("UNIT_MISSING_LLM_API_KEY",)
    assert LLMAPIHandlerFactory.get_llm_api_handler("UNIT_MISSING_ALIAS") is dummy_llm_api_handler


def test_local_module_registration_handles_missing_enabled_provider_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_registry.settings, "ENV", "local")
    monkeypatch.setattr(config_registry.settings, "ENABLE_GEMINI", True)
    monkeypatch.setattr(config_registry.settings, "GEMINI_API_KEY", None)
    monkeypatch.setattr(config_registry.settings, "ENABLE_OPENAI_COMPATIBLE", True)
    monkeypatch.setattr(config_registry.settings, "OPENAI_COMPATIBLE_MODEL_NAME", None)
    monkeypatch.setattr(llm_schemas, "_settings", lambda: config_registry.settings)
    assert config_registry.__file__ is not None

    registry_namespace = runpy.run_path(str(Path(config_registry.__file__)))
    registry = registry_namespace["LLMConfigRegistry"]

    assert registry.get_config_issue("GEMINI_3_PRO") is not None
    assert registry.get_config_issue("GEMINI_3.1_PRO") is not None
    openai_compatible_issue = registry.get_config_issue(config_registry.settings.OPENAI_COMPATIBLE_MODEL_KEY)
    assert openai_compatible_issue is not None
    assert openai_compatible_issue.missing_env_vars == ("OPENAI_COMPATIBLE_MODEL_NAME",)


def test_non_local_registry_still_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_registry.settings, "ENV", "prod")

    with pytest.raises(MissingLLMProviderEnvVarsError):
        LLMConfigRegistry.register_config(
            "UNIT_MISSING",
            LLMConfig("unit/missing", ["UNIT_MISSING_LLM_API_KEY"], supports_vision=True, add_assistant_prefix=False),
        )


@pytest.mark.asyncio
async def test_llm_status_requires_setup_when_default_and_custom_llms_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(internal_llms.settings, "LLM_KEY", "UNIT_DEFAULT")
    monkeypatch.setattr(internal_llms.settings, "LLM_API_KEY", None)
    monkeypatch.setattr(
        internal_llms.app,
        "DATABASE",
        SimpleNamespace(organizations=FakeOrganizationsRepository(organization=None)),
    )

    response = await internal_llms.evaluate_llm_status()

    assert response.status is LLMDiagnosticsStatus.setup_required
    assert response.has_server_configured_llm is False
    assert response.custom_llm_count == 0
    assert response.issues[0].missing_env_vars == ["LLM_API_KEY"]


@pytest.mark.asyncio
async def test_llm_status_is_ok_when_custom_llm_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org = _org()
    monkeypatch.setattr(internal_llms.settings, "LLM_KEY", "UNIT_DEFAULT")
    monkeypatch.setattr(internal_llms.settings, "LLM_API_KEY", None)
    monkeypatch.setattr(
        internal_llms.app,
        "DATABASE",
        SimpleNamespace(organizations=FakeOrganizationsRepository(org, [_custom_llm_token(org.organization_id)])),
    )

    response = await internal_llms.evaluate_llm_status()

    assert response.status is LLMDiagnosticsStatus.ok
    assert response.has_server_configured_llm is False
    assert response.custom_llm_count == 1
