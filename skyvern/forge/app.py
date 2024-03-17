from ddtrace import tracer
from ddtrace.filters import FilterRequestsOnUrl

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.storage.factory import StorageFactory
from skyvern.forge.sdk.db.client import AgentDB
from skyvern.forge.sdk.forge_log import setup_logger
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.webeye.browser_manager import BrowserManager

tracer.configure(
    settings={
        "FILTERS": [
            FilterRequestsOnUrl(r"http://.*/heartbeat$"),
        ],
    },
)

setup_logger()
SETTINGS_MANAGER = SettingsManager.get_settings()
DATABASE = AgentDB(
    SettingsManager.get_settings().DATABASE_STRING, debug_enabled=SettingsManager.get_settings().DEBUG_MODE
)
STORAGE = StorageFactory.get_storage()
ARTIFACT_MANAGER = ArtifactManager()
BROWSER_MANAGER = BrowserManager()
LLM_API_HANDLER = LLMAPIHandlerFactory.get_llm_api_handler(SettingsManager.get_settings().LLM_KEY)
WORKFLOW_CONTEXT_MANAGER = WorkflowContextManager()
WORKFLOW_SERVICE = WorkflowService()
agent = ForgeAgent()

app = agent.get_agent_app()
