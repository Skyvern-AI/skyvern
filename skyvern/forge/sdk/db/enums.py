from enum import StrEnum


class OrganizationAuthTokenType(StrEnum):
    api = "api"


class ScheduleRuleUnit(StrEnum):
    # No support for scheduling every second
    minute = "minute"
    hour = "hour"
    day = "day"
    week = "week"
    month = "month"
    year = "year"
