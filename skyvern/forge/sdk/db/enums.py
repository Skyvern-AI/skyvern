from enum import StrEnum


class OrganizationAuthTokenType(StrEnum):
    api = "api"
    onepassword_service_account = "onepassword_service_account"


class TaskType(StrEnum):
    general = "general"
    validation = "validation"
    action = "action"
