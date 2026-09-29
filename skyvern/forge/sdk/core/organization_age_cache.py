"""Process-wide organization creation times, so any log line that names an organization can carry its age."""

import datetime
import threading

from skyvern.forge.sdk.core.skyvern_context import compute_org_age

# created_at never changes, so entries never go stale; the cap only bounds memory in long-lived processes.
_MAX_ORGANIZATIONS = 50_000
_created_at_by_organization: dict[str, datetime.datetime] = {}
# Only writers take the lock: a lone dict read is atomic, and log processors read on every line.
_write_lock = threading.Lock()


def remember_organization_created_at(organization_id: str, created_at: datetime.datetime | None) -> None:
    if not isinstance(created_at, datetime.datetime) or organization_id in _created_at_by_organization:
        return
    with _write_lock:
        if len(_created_at_by_organization) >= _MAX_ORGANIZATIONS:
            del _created_at_by_organization[next(iter(_created_at_by_organization))]
        _created_at_by_organization[organization_id] = created_at


def is_organization_age_cached(organization_id: str) -> bool:
    return organization_id in _created_at_by_organization


def cached_org_age(organization_id: str) -> int | None:
    created_at = _created_at_by_organization.get(organization_id)
    return None if created_at is None else compute_org_age(created_at)
