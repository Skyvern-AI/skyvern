from __future__ import annotations

from typing import Awaitable, Callable

from anthropic import AsyncAnthropic, AsyncAnthropicBedrock
from fastapi import FastAPI
from openai import AsyncAzureOpenAI, AsyncOpenAI

from testcharmvision.config import Settings
from testcharmvision.forge.agent import ForgeAgent
from testcharmvision.forge.agent_functions import AgentFunction
from testcharmvision.forge.forge_openai_client import ForgeAsyncHttpxClientWrapper
from testcharmvision.forge.sdk.api.azure import AzureClientFactory
from testcharmvision.forge.sdk.api.custom_credential_client import CustomCredentialAPIClient
from testcharmvision.forge.sdk.api.llm.api_handler import LLMAPIHandler
from testcharmvision.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from testcharmvision.forge.sdk.api.real_azure import RealAzureClientFactory
from testcharmvision.forge.sdk.artifact.manager import ArtifactManager
from testcharmvision.forge.sdk.artifact.storage.azure import AzureStorage
from testcharmvision.forge.sdk.artifact.storage.base import BaseStorage
from testcharmvision.forge.sdk.artifact.storage.factory import StorageFactory
from testcharmvision.forge.sdk.artifact.storage.s3 import S3Storage
from testcharmvision.forge.sdk.cache.base import BaseCache
from testcharmvision.forge.sdk.cache.factory import CacheFactory
from testcharmvision.forge.sdk.core.rate_limiter import NoopRateLimiter, RateLimiter
from testcharmvision.forge.sdk.db.agent_db import AgentDB
from testcharmvision.forge.sdk.experimentation.providers import BaseExperimentationProvider, NoOpExperimentationProvider
from testcharmvision.forge.sdk.schemas.credentials import CredentialVaultType
from testcharmvision.forge.sdk.schemas.organizations import AzureClientSecretCredential, Organization
from testcharmvision.forge.sdk.services.credential.azure_credential_vault_service import AzureCredentialVaultService
from testcharmvision.forge.sdk.services.credential.bitwarden_credential_service import BitwardenCredentialVaultService
from testcharmvision.forge.sdk.services.credential.credential_vault_service import CredentialVaultService
from testcharmvision.forge.sdk.services.credential.custom_credential_vault_service import CustomCredentialVaultService
from testcharmvision.forge.sdk.settings_manager import SettingsManager
from testcharmvision.forge.sdk.workflow.context_manager import WorkflowContextManager
from testcharmvision.forge.sdk.workflow.service import WorkflowService
from testcharmvision.services.browser_recording.service import BrowserSessionRecordingService
from testcharmvision.webeye.browser_manager import BrowserManager
from testcharmvision.webeye.default_persistent_sessions_manager import DefaultPersistentSessionsManager
from testcharmvision.webeye.persistent_sessions_manager import PersistentSessionsManager
from testcharmvision.webeye.real_browser_manager import RealBrowserManager
from testcharmvision.webeye.scraper.scraper import ScrapeExcludeFunc


class ForgeApp:
    """Container for shared Forge services"""

    SETTINGS_MANAGER: Settings
    DATABASE: AgentDB
    REPLICA_DATABASE: AgentDB
    STORAGE: BaseStorage
    CACHE: BaseCache
    ARTIFACT_MANAGER: ArtifactManager
    BROWSER_MANAGER: BrowserManager
    EXPERIMENTATION_PROVIDER: BaseExperimentationProvider
    RATE_LIMITER: RateLimiter
    LLM_API_HANDLER: LLMAPIHandler
    OPENAI_CLIENT: AsyncOpenAI | AsyncAzureOpenAI
    ANTHROPIC_CLIENT: AsyncAnthropic | AsyncAnthropicBedrock
    UI_TARS_CLIENT: AsyncOpenAI | None
    AZURE_CLIENT_FACTORY: AzureClientFactory
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
    BROWSER_SESSION_RECORDING_SERVICE: BrowserSessionRecordingService
    BITWARDEN_CREDENTIAL_VAULT_SERVICE: BitwardenCredentialVaultService
    AZURE_CREDENTIAL_VAULT_SERVICE: AzureCredentialVaultService | None
    CUSTOM_CREDENTIAL_VAULT_SERVICE: CustomCredentialVaultService | None
    CREDENTIAL_VAULT_SERVICES: dict[str, CredentialVaultService | None]
    scrape_exclude: ScrapeExcludeFunc | None
    authentication_function: Callable[[str], Awaitable[Organization]] | None
    authenticate_user_function: Callable[[str], Awaitable[str | None]] | None
    setup_api_app: Callable[[FastAPI], None] | None
    api_app_startup_event: Callable[[FastAPI], Awaitable[None]] | None
    api_app_shutdown_event: Callable[[], Awaitable[None]] | None
    agent: ForgeAgent


def create_forge_app() -> ForgeApp:
    """Create and initialize a ForgeApp instance with all services"""
    settings: Settings = SettingsManager.get_settings()

    app = ForgeApp()

    app.SETTINGS_MANAGER = settings

    app.DATABASE = AgentDB(settings.DATABASE_STRING, debug_enabled=settings.DEBUG_MODE)

    if settings.DATABASE_REPLICA_STRING and settings.DATABASE_REPLICA_STRING != settings.DATABASE_STRING:
        app.REPLICA_DATABASE = AgentDB(settings.DATABASE_REPLICA_STRING, debug_enabled=settings.DEBUG_MODE)
    else:
        app.REPLICA_DATABASE = app.DATABASE

    if settings.TESTCHARMVISION_STORAGE_TYPE == "s3":
        StorageFactory.set_storage(S3Storage())
    elif settings.TESTCHARMVISION_STORAGE_TYPE == "azureblob":
        StorageFactory.set_storage(AzureStorage())
    app.STORAGE = StorageFactory.get_storage()
    app.CACHE = CacheFactory.get_cache()
    app.ARTIFACT_MANAGER = ArtifactManager()
    app.BROWSER_MANAGER = RealBrowserManager()
    app.EXPERIMENTATION_PROVIDER = NoOpExperimentationProvider()
    app.RATE_LIMITER = NoopRateLimiter()

    app.LLM_API_HANDLER = LLMAPIHandlerFactory.get_llm_api_handler(settings.LLM_KEY)
    app.OPENAI_CLIENT = AsyncOpenAI(
        api_key=settings.OPENAI_API_KEY or "",
        http_client=ForgeAsyncHttpxClientWrapper(),
    )
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
            http_client=ForgeAsyncHttpxClientWrapper(),
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
    app.PERSISTENT_SESSIONS_MANAGER = DefaultPersistentSessionsManager(database=app.DATABASE)
    app.BROWSER_SESSION_RECORDING_SERVICE = BrowserSessionRecordingService()

    app.AZURE_CLIENT_FACTORY = RealAzureClientFactory()
    app.BITWARDEN_CREDENTIAL_VAULT_SERVICE = BitwardenCredentialVaultService()

    # Azure Credential Vault Service
    # If running a workload on Azure and using workload identity (the common case for AKS or Azure VMs),
    # use DefaultAzureCredential when a client secret is not provided.
    # If explicit credentials are configured use ClientSecretCredential instead.
    if settings.AZURE_CREDENTIAL_VAULT:
        if settings.AZURE_CLIENT_SECRET:
            # Explicit client secret authentication
            azure_vault_client = app.AZURE_CLIENT_FACTORY.create_from_client_secret(
                AzureClientSecretCredential(
                    tenant_id=settings.AZURE_TENANT_ID,  # type: ignore
                    client_id=settings.AZURE_CLIENT_ID,  # type: ignore
                    client_secret=settings.AZURE_CLIENT_SECRET,  # type: ignore
                )
            )
        else:
            # Workload Identity / DefaultAzureCredential
            azure_vault_client = app.AZURE_CLIENT_FACTORY.create_default()

        app.AZURE_CREDENTIAL_VAULT_SERVICE = AzureCredentialVaultService(
            azure_vault_client,
            vault_name=settings.AZURE_CREDENTIAL_VAULT,  # type: ignore[arg-type]
        )
    else:
        app.AZURE_CREDENTIAL_VAULT_SERVICE = None
    app.CUSTOM_CREDENTIAL_VAULT_SERVICE = (
        CustomCredentialVaultService(
            CustomCredentialAPIClient(
                api_base_url=settings.CUSTOM_CREDENTIAL_API_BASE_URL,  # type: ignore
                api_token=settings.CUSTOM_CREDENTIAL_API_TOKEN,  # type: ignore
            )
        )
        if settings.CUSTOM_CREDENTIAL_API_BASE_URL and settings.CUSTOM_CREDENTIAL_API_TOKEN
        else CustomCredentialVaultService()  # Create service without client for organization-based configuration
    )
    app.CREDENTIAL_VAULT_SERVICES = {
        CredentialVaultType.BITWARDEN: app.BITWARDEN_CREDENTIAL_VAULT_SERVICE,
        CredentialVaultType.AZURE_VAULT: app.AZURE_CREDENTIAL_VAULT_SERVICE,
        CredentialVaultType.CUSTOM: app.CUSTOM_CREDENTIAL_VAULT_SERVICE,
    }

    app.scrape_exclude = None
    app.authentication_function = None
    app.authenticate_user_function = None
    app.setup_api_app = None
    app.api_app_startup_event = None
    app.api_app_shutdown_event = None

    app.agent = ForgeAgent()

    return app
