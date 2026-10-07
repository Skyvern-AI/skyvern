from enum import StrEnum
from functools import cache
from zoneinfo import available_timezones

from pydantic import BaseModel, Field


@cache
def iana_timezone_keys() -> frozenset[str]:
    # Debian ships zoneinfo/localtime as a link to /etc/localtime: a host alias browsers reject, not an IANA name.
    return frozenset(available_timezones()) - {"localtime"}


class BrowserSettings(BaseModel):
    timezone_id: str | None = Field(
        default=None,
        description="IANA timezone the browser reports to pages, for example `America/New_York`. Applied when a "
        "browser is created and takes precedence over any timezone implied by the proxy location. Offsets such as "
        "`+03:00` are rejected. Omit or set to null to keep the default behavior.",
        examples=["America/New_York"],
    )


def require_known_timezone(browser_settings: BrowserSettings | None) -> BrowserSettings | None:
    """Only request models call this: a stored zone must stay readable on a host whose tzdata lacks it."""
    timezone_id = requested_timezone_id(browser_settings)
    if timezone_id is not None and timezone_id not in iana_timezone_keys():
        raise ValueError(
            f"timezone_id {timezone_id!r} is not an IANA timezone name. Use a name such as 'America/New_York' "
            "or 'Africa/Kampala'; UTC offsets are not accepted."
        )
    return browser_settings


class BrowserSettingsSource(StrEnum):
    workflow_version = "workflow_version"
    session_request = "session_request"
    existing_session = "existing_session"


class BrowserSettingsStatus(StrEnum):
    verified = "verified"
    mismatch = "mismatch"
    unapplied = "unapplied"
    unknown = "unknown"


EXISTING_SESSION_WARNING = (
    "The requested timezone was not applied because this run used a browser it did not create; "
    "the browser kept its own timezone."
)


class BrowserSettingsReceipt(BaseModel):
    requested_timezone_id: str = Field(description="The timezone that was requested.")
    actual_timezone_id: str | None = Field(
        default=None,
        description="The timezone the browser reported when measured, or null when it could not be measured.",
    )
    source: BrowserSettingsSource = Field(description="Where the requested setting came from.")
    status: BrowserSettingsStatus = Field(
        description="`verified` when the browser reported the requested timezone or an IANA alias of it (Chromium "
        "reports `Asia/Calcutta` for `Asia/Kolkata`), `mismatch` when it reported another zone, `unapplied` when the "
        "browser already existed and was left unchanged, `unknown` when it could not be measured."
    )
    warning: str | None = None


def requested_timezone_id(browser_settings: BrowserSettings | None) -> str | None:
    return browser_settings.timezone_id if browser_settings is not None else None


def build_timezone_receipt(
    requested: str,
    actual: str | None,
    source: BrowserSettingsSource,
    *,
    browser_name_for_requested: str | None = None,
) -> BrowserSettingsReceipt:
    """A browser reports an IANA alias under its own name (Asia/Kolkata as Asia/Calcutta), so matching the
    browser's name for the requested zone also verifies it."""
    if source == BrowserSettingsSource.existing_session:
        status = BrowserSettingsStatus.unapplied
    elif actual is None:
        status = BrowserSettingsStatus.unknown
    elif actual in (requested, browser_name_for_requested):
        status = BrowserSettingsStatus.verified
    else:
        status = BrowserSettingsStatus.mismatch
    return BrowserSettingsReceipt(
        requested_timezone_id=requested,
        actual_timezone_id=actual,
        source=source,
        status=status,
        warning=EXISTING_SESSION_WARNING if status == BrowserSettingsStatus.unapplied else None,
    )
