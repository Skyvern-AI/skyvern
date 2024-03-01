from ddtrace import tracer
from ddtrace.filters import FilterRequestsOnUrl

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.api.open_ai import OpenAIClientManager
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.storage.factory import StorageFactory
from skyvern.forge.sdk.db.client import AgentDB
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.forge_log import setup_logger
from skyvern.forge.sdk.settings_manager import SettingsManager
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
ASYNC_EXECUTOR = AsyncExecutorFactory.get_executor()
ARTIFACT_MANAGER = ArtifactManager()
BROWSER_MANAGER = BrowserManager()
OPENAI_CLIENT = OpenAIClientManager()
WORKFLOW_SERVICE = WorkflowService()
agent = ForgeAgent()

app = agent.get_agent_app()
