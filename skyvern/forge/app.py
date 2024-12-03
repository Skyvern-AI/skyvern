from typing import Awaitable, Callable

from fastapi import FastAPI

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.storage.factory import StorageFactory
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.cache.factory import CacheFactory
from skyvern.forge.sdk.db.client import AgentDB
from skyvern.forge.sdk.experimentation.providers import BaseExperimentationProvider, NoOpExperimentationProvider
from skyvern.forge.sdk.models import Organization
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.webeye.browser_manager import BrowserManager
from skyvern.webeye.scraper.scraper import ScrapeExcludeFunc

SETTINGS_MANAGER = SettingsManager.get_settings()
DATABASE = AgentDB(
    SettingsManager.get_settings().DATABASE_STRING,
    debug_enabled=SettingsManager.get_settings().DEBUG_MODE,
)
if SettingsManager.get_settings().SKYVERN_STORAGE_TYPE == "s3":
    StorageFactory.set_storage(S3Storage())
STORAGE = StorageFactory.get_storage()
CACHE = CacheFactory.get_cache()
ARTIFACT_MANAGER = ArtifactManager()
BROWSER_MANAGER = BrowserManager()
EXPERIMENTATION_PROVIDER: BaseExperimentationProvider = NoOpExperimentationProvider()
LLM_API_HANDLER = LLMAPIHandlerFactory.get_llm_api_handler(SettingsManager.get_settings().LLM_KEY)
SECONDARY_LLM_API_HANDLER = LLMAPIHandlerFactory.get_llm_api_handler(
    SETTINGS_MANAGER.SECONDARY_LLM_KEY if SETTINGS_MANAGER.SECONDARY_LLM_KEY else SETTINGS_MANAGER.LLM_KEY
)
WORKFLOW_CONTEXT_MANAGER = WorkflowContextManager()
WORKFLOW_SERVICE = WorkflowService()
AGENT_FUNCTION = AgentFunction()
scrape_exclude: ScrapeExcludeFunc | None = None
authentication_function: Callable[[str], Awaitable[Organization]] | None = None
setup_api_app: Callable[[FastAPI], None] | None = None

agent = ForgeAgent()
