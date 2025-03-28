from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator

from skyvern.utils.url_validators import validate_url


class ProxyLocation(StrEnum):
    US_CA = "US-CA"
    US_NY = "US-NY"
    US_TX = "US-TX"
    US_FL = "US-FL"
    US_WA = "US-WA"
    RESIDENTIAL = "RESIDENTIAL"
    RESIDENTIAL_ES = "RESIDENTIAL_ES"
    RESIDENTIAL_IE = "RESIDENTIAL_IE"
    RESIDENTIAL_GB = "RESIDENTIAL_GB"
    RESIDENTIAL_IN = "RESIDENTIAL_IN"
    RESIDENTIAL_JP = "RESIDENTIAL_JP"
    RESIDENTIAL_FR = "RESIDENTIAL_FR"
    RESIDENTIAL_DE = "RESIDENTIAL_DE"
    RESIDENTIAL_NZ = "RESIDENTIAL_NZ"
    RESIDENTIAL_ZA = "RESIDENTIAL_ZA"
    RESIDENTIAL_AR = "RESIDENTIAL_AR"
    RESIDENTIAL_ISP = "RESIDENTIAL_ISP"
    NONE = "NONE"


def get_tzinfo_from_proxy(proxy_location: ProxyLocation) -> ZoneInfo | None:
    if proxy_location == ProxyLocation.NONE:
        return None

    if proxy_location == ProxyLocation.US_CA:
        return ZoneInfo("America/Los_Angeles")

    if proxy_location == ProxyLocation.US_NY:
        return ZoneInfo("America/New_York")

    if proxy_location == ProxyLocation.US_TX:
        return ZoneInfo("America/Chicago")

    if proxy_location == ProxyLocation.US_FL:
        return ZoneInfo("America/New_York")

    if proxy_location == ProxyLocation.US_WA:
        return ZoneInfo("America/New_York")

    if proxy_location == ProxyLocation.RESIDENTIAL:
        return ZoneInfo("America/New_York")

    if proxy_location == ProxyLocation.RESIDENTIAL_ES:
        return ZoneInfo("Europe/Madrid")

    if proxy_location == ProxyLocation.RESIDENTIAL_IE:
        return ZoneInfo("Europe/Dublin")

    if proxy_location == ProxyLocation.RESIDENTIAL_GB:
        return ZoneInfo("Europe/London")

    if proxy_location == ProxyLocation.RESIDENTIAL_IN:
        return ZoneInfo("Asia/Kolkata")

    if proxy_location == ProxyLocation.RESIDENTIAL_JP:
        return ZoneInfo("Asia/Tokyo")

    if proxy_location == ProxyLocation.RESIDENTIAL_FR:
        return ZoneInfo("Europe/Paris")

    if proxy_location == ProxyLocation.RESIDENTIAL_DE:
        return ZoneInfo("Europe/Berlin")

    if proxy_location == ProxyLocation.RESIDENTIAL_NZ:
        return ZoneInfo("Pacific/Auckland")

    if proxy_location == ProxyLocation.RESIDENTIAL_ZA:
        return ZoneInfo("Africa/Johannesburg")

    if proxy_location == ProxyLocation.RESIDENTIAL_AR:
        return ZoneInfo("America/Argentina/Buenos_Aires")

    if proxy_location == ProxyLocation.RESIDENTIAL_ISP:
        return ZoneInfo("America/New_York")

    return None


class RunEngine(StrEnum):
    skyvern_v1 = "skyvern-1.0"
    skyvern_v2 = "skyvern-2.0"


class TaskRunStatus(StrEnum):
    created = "created"
    queued = "queued"
    running = "running"
    timed_out = "timed_out"
    failed = "failed"
    terminated = "terminated"
    completed = "completed"
    canceled = "canceled"


class TaskRunRequest(BaseModel):
    goal: str
    url: str | None = None
    title: str | None = None
    engine: RunEngine = RunEngine.skyvern_v1
    proxy_location: ProxyLocation | None = None
    data_extraction_schema: dict | list | str | None = None
    error_code_mapping: dict[str, str] | None = None
    max_steps: int | None = None
    webhook_url: str | None = None
    totp_identifier: str | None = None
    totp_url: str | None = None
    browser_session_id: str | None = None
    publish_workflow: bool = False

    @field_validator("url", "webhook_url", "totp_url")
    @classmethod
    def validate_urls(cls, url: str | None) -> str | None:
        if url is None:
            return None

        return validate_url(url)


class TaskRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: str
    engine: RunEngine = RunEngine.skyvern_v1
    status: TaskRunStatus
    goal: str | None = None
    url: str | None = None
    output: dict | list | str | None = None
    failure_reason: str | None = None
    webhook_url: str | None = None
    totp_identifier: str | None = None
    totp_url: str | None = None
    proxy_location: ProxyLocation | None = None
    error_code_mapping: dict[str, str] | None = None
    data_extraction_schema: dict | list | str | None = None
    title: str | None = None
    max_steps: int | None = None
    created_at: datetime
    modified_at: datetime
