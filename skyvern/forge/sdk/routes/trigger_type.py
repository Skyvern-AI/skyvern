from skyvern.constants import SKYVERN_MCP_USER_AGENT, SKYVERN_UI_USER_AGENT
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType

_MANUAL_USER_AGENTS = frozenset({SKYVERN_UI_USER_AGENT, SKYVERN_MCP_USER_AGENT})


def workflow_run_trigger_type_from_user_agent(x_user_agent: str | None) -> WorkflowRunTriggerType:
    if x_user_agent in _MANUAL_USER_AGENTS:
        return WorkflowRunTriggerType.manual
    return WorkflowRunTriggerType.api
