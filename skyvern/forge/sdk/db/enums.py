from enum import StrEnum


class OrganizationAuthTokenType(StrEnum):
    api = "api"
    onepassword_service_account = "onepassword_service_account"
    azure_client_secret_credential = "azure_client_secret_credential"
    custom_credential_service = "custom_credential_service"
    bitwarden_credential = "bitwarden_credential"


class TaskType(StrEnum):
    general = "general"
    validation = "validation"
    action = "action"


class WorkflowRunTriggerType(StrEnum):
    """How a workflow run was initiated.

    - manual: User clicked "Run" in the UI
    - api: Direct API call to the run endpoint
    - scheduled: Triggered by a cron schedule
    - webhook: Triggered by an external system via the webhook endpoint
    """

    manual = "manual"
    api = "api"
    scheduled = "scheduled"
    webhook = "webhook"
