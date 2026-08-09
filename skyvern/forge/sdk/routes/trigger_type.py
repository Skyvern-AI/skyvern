from skyvern.constants import SKYVERN_MCP_USER_AGENT, SKYVERN_UI_USER_AGENT
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType


def workflow_run_trigger_type_from_user_agent(x_user_agent: str | None) -> WorkflowRunTriggerType:
    if x_user_agent == SKYVERN_UI_USER_AGENT:
        return WorkflowRunTriggerType.manual
    if x_user_agent == SKYVERN_MCP_USER_AGENT:
        return WorkflowRunTriggerType.mcp
    return WorkflowRunTriggerType.api


def caps_run_response_values(x_user_agent: str | None) -> bool:
    """Whether a run-detail read should bound its output values (SKY-13015).

    Only the app bounds them: it renders outputs with ``JSON.stringify`` on the main
    thread, so a multi-megabyte value freezes the page. Programmatic callers — SDK,
    webhooks, replay — get the stored value in full. MCP is bounded separately at its
    own, much lower, response ceiling.
    """
    return x_user_agent == SKYVERN_UI_USER_AGENT
