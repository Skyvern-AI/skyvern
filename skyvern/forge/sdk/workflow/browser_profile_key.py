from __future__ import annotations

from hashlib import sha256
from typing import Any

from jinja2.sandbox import SandboxedEnvironment

_JINJA_ENV = SandboxedEnvironment()
_PROFILE_SEGMENT_PATH = "profile_segments"
_PROFILE_SEGMENT_DIGEST_LENGTH = 24


def normalize_browser_profile_key(browser_profile_key: str | None) -> str | None:
    if browser_profile_key is None:
        return None
    stripped = browser_profile_key.strip()
    return stripped or None


def validate_browser_profile_key(browser_profile_key: str | None) -> str | None:
    normalized = normalize_browser_profile_key(browser_profile_key)
    if normalized is None:
        return None
    _JINJA_ENV.parse(normalized)
    return normalized


def render_browser_profile_key(browser_profile_key: str | None, parameters: dict[str, Any]) -> str | None:
    normalized = normalize_browser_profile_key(browser_profile_key)
    if normalized is None:
        return None
    rendered = _JINJA_ENV.from_string(normalized).render(parameters)
    return rendered.strip() or None


def build_workflow_browser_session_storage_key(
    workflow_permanent_id: str,
    rendered_browser_profile_key: str | None,
) -> str:
    rendered = rendered_browser_profile_key.strip() if rendered_browser_profile_key else ""
    if not rendered:
        return workflow_permanent_id
    digest = sha256(rendered.encode("utf-8")).hexdigest()[:_PROFILE_SEGMENT_DIGEST_LENGTH]
    return f"{workflow_permanent_id}/{_PROFILE_SEGMENT_PATH}/{digest}"
