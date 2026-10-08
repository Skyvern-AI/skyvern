from enum import StrEnum
from typing import Literal, TypeAlias


class OrganizationAuthTokenType(StrEnum):
    api = "api"
    ui_session = "ui_session"
    onepassword_service_account = "onepassword_service_account"
    azure_client_secret_credential = "azure_client_secret_credential"
    custom_credential_service = "custom_credential_service"
    bitwarden_credential = "bitwarden_credential"
    custom_llm = "custom_llm"
    google_oauth_client_config = "google_oauth_client_config"
    twilio_credential = "twilio_credential"


class TaskType(StrEnum):
    general = "general"
    validation = "validation"
    action = "action"
    synthetic_sdk_action = "synthetic_sdk_action"


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
    - job_recipe_extract: Launched by a job recipe extract request
    - job_recipe_apply: Launched by a job recipe apply request
    """

    manual = "manual"
    mcp = "mcp"
    api = "api"
    scheduled = "scheduled"
    webhook = "webhook"
    job_recipe_extract = "job_recipe_extract"
    job_recipe_apply = "job_recipe_apply"


MANUAL_LIKE_WORKFLOW_RUN_TRIGGER_TYPES = frozenset(
    {
        WorkflowRunTriggerType.manual,
        WorkflowRunTriggerType.mcp,
    }
)


def is_manual_like_workflow_run_trigger_type(trigger_type: WorkflowRunTriggerType | None) -> bool:
    return trigger_type in MANUAL_LIKE_WORKFLOW_RUN_TRIGGER_TYPES


JOB_RECIPE_WORKFLOW_RUN_TRIGGER_TYPES = frozenset(
    {
        WorkflowRunTriggerType.job_recipe_extract,
        WorkflowRunTriggerType.job_recipe_apply,
    }
)


def is_job_recipe_workflow_run_trigger_type(trigger_type: WorkflowRunTriggerType | None) -> bool:
    return trigger_type in JOB_RECIPE_WORKFLOW_RUN_TRIGGER_TYPES


class WorkflowRunStatus(StrEnum):
    created = "created"
    queued = "queued"
    running = "running"
    failed = "failed"
    terminated = "terminated"
    canceled = "canceled"
    timed_out = "timed_out"
    completed = "completed"
    paused = "paused"

    def is_final(self) -> bool:
        return self in [
            WorkflowRunStatus.failed,
            WorkflowRunStatus.terminated,
            WorkflowRunStatus.canceled,
            WorkflowRunStatus.timed_out,
            WorkflowRunStatus.completed,
        ]

    def is_final_excluding_canceled(self) -> bool:
        """Like :meth:`is_final` but excludes ``canceled``.

        For callers that can't distinguish a legitimate user/block cancel from
        a synthetic ``canceled`` written as a last-resort fallback — e.g. the
        copilot tool reading the row AFTER ``mark_workflow_run_as_canceled_if_not_final``
        has run. Callers that want to trust a legitimate ``canceled`` must read
        the row BEFORE invoking any cancel helper.
        """
        return self.is_final() and self is not WorkflowRunStatus.canceled


# An alias, not an inline Literal at each use: the per-file pre-commit mypy run (follow_imports = skip) sees an
# imported WorkflowRunStatus as Any, and Literal[Any] is a valid-type error there.
DispatchFinalizationStatus: TypeAlias = Literal[WorkflowRunStatus.failed, WorkflowRunStatus.timed_out]
