from enum import StrEnum


class OrganizationAuthTokenType(StrEnum):
    api = "api"


class TaskType(StrEnum):
    general = "general"
    validation = "validation"
    action = "action"
