import re

import structlog

LOG = structlog.get_logger()

VALID_RUN_WITH_VALUES = ("agent", "code")
# Known legacy values in prod: 'ai' (-> 'agent'), 'code_v2' (-> 'code')
_LEGACY_RUN_WITH_MAP = {"ai": "agent", "code_v2": "code"}

RUN_METADATA_MAX_KEYS = 20
RUN_METADATA_MAX_KEY_LENGTH = 64
RUN_METADATA_MAX_VALUE_LENGTH = 256

TAG_DESCRIPTION_MAX_LENGTH = 500
# Reserved for system-written tags. Uses a regex-valid separator so the
# reservation actually excludes keys a user could otherwise create.
SKYVERN_TAG_NAMESPACE = "skyvern."
# Anchored regex: keys must be URL-safe so they round-trip through path
# (`DELETE .../tags/{key}`) and query (`?tags=k:v`) without escaping.
TAG_KEY_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# `,` separates pairs in the `?tags=k:v,k2:v2` filter encoding, so it may not
# appear inside a value. `:` is allowed: the parser splits on the first `:` only.
_TAG_VALUE_FORBIDDEN_CHARS = (",",)


def normalize_run_with(v: str | None) -> str:
    """Normalize null and legacy values to valid run_with values.

    Never raises — unknown values fall back to 'agent' with a warning
    so that one bad DB row can't crash the entire workflow listing.
    """
    if v is None:
        return "agent"
    if v in _LEGACY_RUN_WITH_MAP:
        return _LEGACY_RUN_WITH_MAP[v]
    if v not in VALID_RUN_WITH_VALUES:
        LOG.warning("Unknown run_with value, defaulting to 'agent'", run_with=v)
        return "agent"
    return v


def _normalize_kv_dict(
    v: dict[str, str] | None,
    *,
    max_keys: int,
    max_key_length: int,
    max_value_length: int,
    field_name: str,
) -> dict[str, str] | None:
    """Shared cap + trim + skip-empty pass over a str→str dict.

    Caller layers extra rules (regex, namespace, value-char restrictions)
    on top of the returned dict. Returns None when the input is None or
    every entry was skipped as empty.
    """
    if v is None:
        return None

    if len(v) > max_keys:
        raise ValueError(f"{field_name} can include at most {max_keys} entries")

    normalized: dict[str, str] = {}
    for key, value in v.items():
        trimmed_key = key.strip()
        trimmed_value = value.strip()
        if not trimmed_key or not trimmed_value:
            continue
        if len(trimmed_key) > max_key_length:
            raise ValueError(f"{field_name} keys must be at most {max_key_length} characters")
        if len(trimmed_value) > max_value_length:
            raise ValueError(f"{field_name} values must be at most {max_value_length} characters")
        normalized[trimmed_key] = trimmed_value

    return normalized or None


def normalize_run_metadata(v: dict[str, str] | None) -> dict[str, str] | None:
    """Normalize and bound user-supplied workflow run metadata tags."""
    return _normalize_kv_dict(
        v,
        max_keys=RUN_METADATA_MAX_KEYS,
        max_key_length=RUN_METADATA_MAX_KEY_LENGTH,
        max_value_length=RUN_METADATA_MAX_VALUE_LENGTH,
        field_name="run_metadata",
    )


def normalize_tags(v: dict[str, str] | None) -> dict[str, str] | None:
    """Normalize and bound user-supplied workflow tags.

    Stricter than normalize_run_metadata: keys must match a URL-safe regex
    and may not start with the reserved ``skyvern.`` prefix, and values may
    not contain ``,`` (which would break ``?tags=`` pair parsing).
    """
    normalized = _normalize_kv_dict(
        v,
        max_keys=RUN_METADATA_MAX_KEYS,
        max_key_length=RUN_METADATA_MAX_KEY_LENGTH,
        max_value_length=RUN_METADATA_MAX_VALUE_LENGTH,
        field_name="tags",
    )
    if normalized is None:
        return None

    for key, value in normalized.items():
        if key.startswith(SKYVERN_TAG_NAMESPACE):
            raise ValueError(f"tags keys must not start with the reserved '{SKYVERN_TAG_NAMESPACE}' prefix")
        if not TAG_KEY_REGEX.match(key):
            raise ValueError(
                "tags keys must match '^[A-Za-z0-9][A-Za-z0-9_.-]*$' "
                "(alphanumeric, underscore, dot, hyphen; must start with alphanumeric)"
            )
        for forbidden in _TAG_VALUE_FORBIDDEN_CHARS:
            if forbidden in value:
                raise ValueError(f"tags values must not contain '{forbidden}'")

    return normalized


def normalize_tag_description(v: str | None) -> str | None:
    """Trim and length-bound a tag-key description; return None when empty."""
    if v is None:
        return None
    trimmed = v.strip()
    if not trimmed:
        return None
    if len(trimmed) > TAG_DESCRIPTION_MAX_LENGTH:
        raise ValueError(f"tag description must be at most {TAG_DESCRIPTION_MAX_LENGTH} characters")
    return trimmed
