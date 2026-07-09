from skyvern.constants import SKYVERN_MCP_USER_AGENT, SKYVERN_UI_USER_AGENT
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType


def workflow_run_trigger_type_from_user_agent(x_user_agent: str | None) -> WorkflowRunTriggerType:
    if x_user_agent == SKYVERN_UI_USER_AGENT:
        return WorkflowRunTriggerType.manual
    if x_user_agent == SKYVERN_MCP_USER_AGENT:
        return WorkflowRunTriggerType.mcp
    return WorkflowRunTriggerType.api
