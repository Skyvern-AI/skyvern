from __future__ import annotations

from typing import Awaitable, Callable

from anthropic import AsyncAnthropic, AsyncAnthropicBedrock
from fastapi import FastAPI
from openai import AsyncAzureOpenAI, AsyncOpenAI

from skyvern.config import Settings
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.api.llm.models import LLMAPIHandler
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.storage.base import BaseStorage
from skyvern.forge.sdk.artifact.storage.factory import StorageFactory
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.cache.base import BaseCache
from skyvern.forge.sdk.cache.factory import CacheFactory
from skyvern.forge.sdk.db.client import AgentDB
from skyvern.forge.sdk.experimentation.providers import BaseExperimentationProvider, NoOpExperimentationProvider
from skyvern.forge.sdk.schemas.credentials import CredentialVaultType
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services.credential.azure_credential_vault_service import AzureCredentialVaultService
from skyvern.forge.sdk.services.credential.bitwarden_credential_service import BitwardenCredentialVaultService
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.webeye.browser_manager import BrowserManager
from skyvern.webeye.persistent_sessions_manager import PersistentSessionsManager
from skyvern.webeye.scraper.scraper import ScrapeExcludeFunc


class ForgeApp:
    """Container for shared Forge services"""

    SETTINGS_MANAGER: Settings
    DATABASE: AgentDB
    STORAGE: BaseStorage
    CACHE: BaseCache
    ARTIFACT_MANAGER: ArtifactManager
    BROWSER_MANAGER: BrowserManager
    EXPERIMENTATION_PROVIDER: BaseExperimentationProvider
    LLM_API_HANDLER: LLMAPIHandler
    OPENAI_CLIENT: AsyncOpenAI | AsyncAzureOpenAI
    ANTHROPIC_CLIENT: AsyncAnthropic | AsyncAnthropicBedrock
    UI_TARS_CLIENT: AsyncOpenAI | None
    SECONDARY_LLM_API_HANDLER: LLMAPIHandler
    SELECT_AGENT_LLM_API_HANDLER: LLMAPIHandler
    NORMAL_SELECT_AGENT_LLM_API_HANDLER: LLMAPIHandler
    CUSTOM_SELECT_AGENT_LLM_API_HANDLER: LLMAPIHandler
    SINGLE_CLICK_AGENT_LLM_API_HANDLER: LLMAPIHandler
    SINGLE_INPUT_AGENT_LLM_API_HANDLER: LLMAPIHandler
    PARSE_SELECT_LLM_API_HANDLER: LLMAPIHandler
    EXTRACTION_LLM_API_HANDLER: LLMAPIHandler
    CHECK_USER_GOAL_LLM_API_HANDLER: LLMAPIHandler
    AUTO_COMPLETION_LLM_API_HANDLER: LLMAPIHandler
    SVG_CSS_CONVERTER_LLM_API_HANDLER: LLMAPIHandler | None
    SCRIPT_GENERATION_LLM_API_HANDLER: LLMAPIHandler
    WORKFLOW_CONTEXT_MANAGER: WorkflowContextManager
    WORKFLOW_SERVICE: WorkflowService
    AGENT_FUNCTION: AgentFunction
    PERSISTENT_SESSIONS_MANAGER: PersistentSessionsManager
    BITWARDEN_CREDENTIAL_VAULT_SERVICE: BitwardenCredentialVaultService
    AZURE_CREDENTIAL_VAULT_SERVICE: AzureCredentialVaultService | None
    CREDENTIAL_VAULT_SERVICES: dict[str, CredentialVaultService | None]
    scrape_exclude: ScrapeExcludeFunc | None
    authentication_function: Callable[[str], Awaitable[Organization]] | None
    authenticate_user_function: Callable[[str], Awaitable[str | None]] | None
    setup_api_app: Callable[[FastAPI], None] | None
    api_app_startup_event: Callable[[], Awaitable[None]] | None
    api_app_shutdown_event: Callable[[], Awaitable[None]] | None
    agent: ForgeAgent


def create_forge_app() -> ForgeApp:
    """Create and initialize a ForgeApp instance with all services"""
    settings: Settings = SettingsManager.get_settings()

    app = ForgeApp()

    app.SETTINGS_MANAGER = settings

    app.DATABASE = AgentDB(settings.DATABASE_STRING, debug_enabled=settings.DEBUG_MODE)
    if settings.SKYVERN_STORAGE_TYPE == "s3":
        StorageFactory.set_storage(S3Storage())
    app.STORAGE = StorageFactory.get_storage()
    app.CACHE = CacheFactory.get_cache()
    app.ARTIFACT_MANAGER = ArtifactManager()
    app.BROWSER_MANAGER = BrowserManager()
    app.EXPERIMENTATION_PROVIDER = NoOpExperimentationProvider()

    app.LLM_API_HANDLER = LLMAPIHandlerFactory.get_llm_api_handler(settings.LLM_KEY)
    app.OPENAI_CLIENT = AsyncOpenAI(api_key=settings.OPENAI_API_KEY or "")
    if settings.ENABLE_AZURE_CUA:
        app.OPENAI_CLIENT = AsyncAzureOpenAI(
            api_key=settings.AZURE_CUA_API_KEY,
            api_version=settings.AZURE_CUA_API_VERSION,
            azure_endpoint=settings.AZURE_CUA_ENDPOINT,
            azure_deployment=settings.AZURE_CUA_DEPLOYMENT,
        )

    app.ANTHROPIC_CLIENT = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    if settings.ENABLE_BEDROCK_ANTHROPIC:
        app.ANTHROPIC_CLIENT = AsyncAnthropicBedrock()

    app.UI_TARS_CLIENT = None
    if settings.ENABLE_VOLCENGINE:
        app.UI_TARS_CLIENT = AsyncOpenAI(
            api_key=settings.VOLCENGINE_API_KEY,
            base_url=settings.VOLCENGINE_API_BASE,
        )

    app.SECONDARY_LLM_API_HANDLER = LLMAPIHandlerFactory.get_llm_api_handler(
        settings.SECONDARY_LLM_KEY if settings.SECONDARY_LLM_KEY else settings.LLM_KEY
    )
    app.SELECT_AGENT_LLM_API_HANDLER = LLMAPIHandlerFactory.get_llm_api_handler(
        settings.SELECT_AGENT_LLM_KEY or settings.SECONDARY_LLM_KEY or settings.LLM_KEY
    )
    app.NORMAL_SELECT_AGENT_LLM_API_HANDLER = (
        LLMAPIHandlerFactory.get_llm_api_handler(settings.NORMAL_SELECT_AGENT_LLM_KEY)
        if settings.NORMAL_SELECT_AGENT_LLM_KEY
        else app.SECONDARY_LLM_API_HANDLER
    )
    app.CUSTOM_SELECT_AGENT_LLM_API_HANDLER = (
        LLMAPIHandlerFactory.get_llm_api_handler(settings.CUSTOM_SELECT_AGENT_LLM_KEY)
        if settings.CUSTOM_SELECT_AGENT_LLM_KEY
        else app.SECONDARY_LLM_API_HANDLER
    )
    app.SINGLE_CLICK_AGENT_LLM_API_HANDLER = LLMAPIHandlerFactory.get_llm_api_handler(
        settings.SINGLE_CLICK_AGENT_LLM_KEY or settings.SECONDARY_LLM_KEY or settings.LLM_KEY
    )
    app.SINGLE_INPUT_AGENT_LLM_API_HANDLER = LLMAPIHandlerFactory.get_llm_api_handler(
        settings.SINGLE_INPUT_AGENT_LLM_KEY or settings.SECONDARY_LLM_KEY or settings.LLM_KEY
    )
    app.PARSE_SELECT_LLM_API_HANDLER = (
        LLMAPIHandlerFactory.get_llm_api_handler(settings.PARSE_SELECT_LLM_KEY)
        if settings.PARSE_SELECT_LLM_KEY
        else app.SECONDARY_LLM_API_HANDLER
    )
    app.EXTRACTION_LLM_API_HANDLER = (
        LLMAPIHandlerFactory.get_llm_api_handler(settings.EXTRACTION_LLM_KEY)
        if settings.EXTRACTION_LLM_KEY
        else app.LLM_API_HANDLER
    )
    app.CHECK_USER_GOAL_LLM_API_HANDLER = (
        LLMAPIHandlerFactory.get_llm_api_handler(settings.CHECK_USER_GOAL_LLM_KEY)
        if settings.CHECK_USER_GOAL_LLM_KEY
        else app.SECONDARY_LLM_API_HANDLER
    )
    app.AUTO_COMPLETION_LLM_API_HANDLER = (
        LLMAPIHandlerFactory.get_llm_api_handler(settings.AUTO_COMPLETION_LLM_KEY)
        if settings.AUTO_COMPLETION_LLM_KEY
        else app.SECONDARY_LLM_API_HANDLER
    )
    app.SVG_CSS_CONVERTER_LLM_API_HANDLER = app.SECONDARY_LLM_API_HANDLER if settings.SECONDARY_LLM_KEY else None
    app.SCRIPT_GENERATION_LLM_API_HANDLER = (
        LLMAPIHandlerFactory.get_llm_api_handler(settings.SCRIPT_GENERATION_LLM_KEY)
        if settings.SCRIPT_GENERATION_LLM_KEY
        else app.SECONDARY_LLM_API_HANDLER
    )

    app.WORKFLOW_CONTEXT_MANAGER = WorkflowContextManager()
    app.WORKFLOW_SERVICE = WorkflowService()
    app.AGENT_FUNCTION = AgentFunction()
    app.PERSISTENT_SESSIONS_MANAGER = PersistentSessionsManager(database=app.DATABASE)

    app.BITWARDEN_CREDENTIAL_VAULT_SERVICE = BitwardenCredentialVaultService()
    app.AZURE_CREDENTIAL_VAULT_SERVICE = (
        AzureCredentialVaultService(
            tenant_id=settings.AZURE_TENANT_ID,  # type: ignore[arg-type]
            client_id=settings.AZURE_CLIENT_ID,  # type: ignore[arg-type]
            client_secret=settings.AZURE_CLIENT_SECRET,  # type: ignore[arg-type]
            vault_name=settings.AZURE_CREDENTIAL_VAULT,  # type: ignore[arg-type]
        )
        if settings.AZURE_CREDENTIAL_VAULT
        else None
    )
    app.CREDENTIAL_VAULT_SERVICES = {
        CredentialVaultType.BITWARDEN: app.BITWARDEN_CREDENTIAL_VAULT_SERVICE,
        CredentialVaultType.AZURE_VAULT: app.AZURE_CREDENTIAL_VAULT_SERVICE,
    }

    app.scrape_exclude = None
    app.authentication_function = None
    app.authenticate_user_function = None
    app.setup_api_app = None
    app.api_app_startup_event = None
    app.api_app_shutdown_event = None

    app.agent = ForgeAgent()

    return app
