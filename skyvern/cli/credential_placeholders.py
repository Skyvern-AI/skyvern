"""Shared placeholder/sentinel handling for credentials."""

from __future__ import annotations

CREDENTIAL_PLACEHOLDERS: tuple[str, ...] = (
    "",
    "PLACEHOLDER",
    "YOUR_API_KEY",
)

FRONTEND_BUNDLE_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "__VITE_API_BASE_URL_PLACEHOLDER__",
        "__VITE_WSS_BASE_URL_PLACEHOLDER__",
        "__VITE_ARTIFACT_API_BASE_URL_PLACEHOLDER__",
        "__SKYVERN_API_KEY_PLACEHOLDER__",
    }
)


def is_placeholder_credential_value(value: str) -> bool:
    """Return true when an env-like credential value is a known unusable placeholder."""
    normalized = value.strip()
    return (
        normalized in CREDENTIAL_PLACEHOLDERS
        or normalized in FRONTEND_BUNDLE_PLACEHOLDERS
        or (normalized.startswith("__") and normalized.endswith("__") and "PLACEHOLDER" in normalized)
    )


_FRONTEND_API_KEY_PLACEHOLDERS = frozenset(CREDENTIAL_PLACEHOLDERS) - {"PLACEHOLDER"}


def is_frontend_api_key_placeholder(value: str) -> bool:
    """Return true when an env API key should be treated as missing by doctor checks."""
    normalized = value.strip()
    return normalized in _FRONTEND_API_KEY_PLACEHOLDERS
