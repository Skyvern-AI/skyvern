from collections.abc import Sequence
from typing import Any

import structlog

LOG = structlog.get_logger(__name__)

WILDCARD_ORIGIN = "*"


def cors_allows_any_origin(allowed_origins: Sequence[str]) -> bool:
    return any(origin.strip() == WILDCARD_ORIGIN for origin in allowed_origins)


def cors_middleware_kwargs(
    allowed_origins: Sequence[str],
    allowed_origin_regex: str | None,
    expose_headers: list[str] | None = None,
) -> dict[str, Any]:
    """Build the CORSMiddleware kwargs for the app-level CORS policy.

    An exact "*" (the self-hosted default) allows every origin, but never with credentials:
    wildcard + allow_credentials lets any site send cookie-authenticated requests.
    Header-based API-key auth does not need CORS credentials.
    """
    kwargs: dict[str, Any]
    if cors_allows_any_origin(allowed_origins):
        discarded_origins = [
            stripped for origin in allowed_origins if (stripped := origin.strip()) and stripped != WILDCARD_ORIGIN
        ]
        LOG.warning(
            "Wildcard CORS origin configured; allowing every origin without credentials",
            discarded_origin_count=len(discarded_origins),
            origin_regex_configured=credentialed_cors_allow_origin_regex(allowed_origin_regex) is not None,
        )
        kwargs = {
            "allow_origins": [WILDCARD_ORIGIN],
            "allow_methods": ["*"],
            "allow_headers": ["*"],
        }
    else:
        kwargs = {
            "allow_origins": credentialed_cors_allow_origins(allowed_origins),
            "allow_credentials": True,
            "allow_methods": ["*"],
            "allow_headers": ["*"],
            "allow_origin_regex": credentialed_cors_allow_origin_regex(allowed_origin_regex),
        }
    if expose_headers is not None:
        kwargs["expose_headers"] = expose_headers
    return kwargs


def credentialed_cors_allow_origins(allowed_origins: Sequence[str]) -> list[str]:
    wildcard_origin_count = 0
    credentialed_origins: list[str] = []

    for origin in allowed_origins:
        stripped_origin = origin.strip()
        if not stripped_origin:
            continue
        if "*" in stripped_origin:
            wildcard_origin_count += 1
            continue
        credentialed_origins.append(stripped_origin)

    if wildcard_origin_count:
        LOG.warning(
            "Ignoring wildcard CORS origins for credentialed requests",
            wildcard_origin_count=wildcard_origin_count,
        )

    return credentialed_origins


def credentialed_cors_allow_origin_regex(allowed_origin_regex: str | None) -> str | None:
    if allowed_origin_regex is None:
        return None

    stripped_origin_regex = allowed_origin_regex.strip()
    return stripped_origin_regex or None
