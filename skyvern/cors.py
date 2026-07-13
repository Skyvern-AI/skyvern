from collections.abc import Sequence

import structlog

LOG = structlog.get_logger(__name__)


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
