from enum import StrEnum


class OrganizationAuthTokenType(StrEnum):
    api = "api"
    onepassword_service_account = "onepassword_service_account"
    azure_client_secret_credential = "azure_client_secret_credential"
    custom_credential_service = "custom_credential_service"


class TaskType(StrEnum):
    general = "general"
    validation = "validation"
    action = "action"
