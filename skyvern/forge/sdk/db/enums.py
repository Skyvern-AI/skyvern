from enum import StrEnum


class OrganizationAuthTokenType(StrEnum):
    api = "api"
    onepassword_service_account = "onepassword_service_account"
    azure_client_secret_credential = "azure_client_secret_credential"
    custom_credential_service = "custom_credential_service"
    bitwarden_credential = "bitwarden_credential"
    custom_llm = "custom_llm"
    google_oauth_client_config = "google_oauth_client_config"


class TaskType(StrEnum):
    general = "general"
    validation = "validation"
    action = "action"


class BrowserSeedSource(StrEnum):
    """Which layer of the seed-precedence chain seeded a run's browser (provenance).

    Resolved once at run setup, before any browser creation, for all run types (C-semantics).
    - override: explicit request browser_profile_id (one-run-only pick via API)
    - picked: the workflow's explicit profile pick (workflows.browser_profile_id) — "always start here"
    - own_memory: the workflow's own auto-profile (no pick + persist_browser_session)
    - credential: the run's selected credential's profile (rotation-aware; also the empty-own boot)
    - fresh: no seed profile
    - degraded_fresh: a resolved profile failed to load; ran fresh
    """

    override = "override"
    picked = "picked"
    own_memory = "own_memory"
    credential = "credential"
    fresh = "fresh"
    degraded_fresh = "degraded_fresh"


class WorkflowRunTriggerType(StrEnum):
    """How a workflow run was initiated.

    - manual: User clicked "Run" in the UI
    - mcp: First-party MCP client request
    - api: Direct API call to the run endpoint
    - scheduled: Triggered by a cron schedule
    - webhook: Triggered by an external system via the webhook endpoint
    """

    manual = "manual"
    mcp = "mcp"
    api = "api"
    scheduled = "scheduled"
    webhook = "webhook"


MANUAL_LIKE_WORKFLOW_RUN_TRIGGER_TYPES = frozenset(
    {
        WorkflowRunTriggerType.manual,
        WorkflowRunTriggerType.mcp,
    }
)


def is_manual_like_workflow_run_trigger_type(trigger_type: WorkflowRunTriggerType | None) -> bool:
    return trigger_type in MANUAL_LIKE_WORKFLOW_RUN_TRIGGER_TYPES
