import random
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
# A tag is a label (value, always present) with an optional group (key). Keys
# and values inherit the run_metadata length caps.
TAG_KEY_MAX_LENGTH = RUN_METADATA_MAX_KEY_LENGTH
TAG_VALUE_MAX_LENGTH = RUN_METADATA_MAX_VALUE_LENGTH
# Reserved for system-written tags. Uses a regex-valid separator so the
# reservation actually excludes keys a user could otherwise create.
SKYVERN_TAG_NAMESPACE = "skyvern."
# Anchored regex: keys must be URL-safe so they round-trip through path
# (`DELETE .../tags/{key}`) and query (`?tags=k:v`) without escaping.
TAG_KEY_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# `,` separates terms in the `?tags=` filter encoding, so it may not appear
# inside a value. `:` is allowed: the parser splits on the first `:` only.
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


def _assert_valid_tag_key(key: str) -> None:
    """Shape rules for a (trimmed, non-empty) tag key / group. Raises ValueError."""
    if len(key) > TAG_KEY_MAX_LENGTH:
        raise ValueError(f"tag keys must be at most {TAG_KEY_MAX_LENGTH} characters")
    if key.startswith(SKYVERN_TAG_NAMESPACE):
        raise ValueError(f"tag keys must not start with the reserved '{SKYVERN_TAG_NAMESPACE}' prefix")
    if not TAG_KEY_REGEX.match(key):
        raise ValueError(
            "tag keys must match '^[A-Za-z0-9][A-Za-z0-9_.-]*$' "
            "(alphanumeric, underscore, dot, hyphen; must start with alphanumeric)"
        )


def normalize_optional_tag_key(key: object) -> str | None:
    """Normalize an optional tag key (group). None or blank -> None (standalone
    label); otherwise trim and enforce the key shape rules."""
    if key is None:
        return None
    if not isinstance(key, str):
        raise ValueError("tag key must be a string")
    trimmed = key.strip()
    if not trimmed:
        return None
    _assert_valid_tag_key(trimmed)
    return trimmed


def normalize_tag_value(value: object) -> str:
    """Normalize a required tag value (label). Trims, enforces the length cap,
    and rejects ``,`` (which would break ``?tags=`` term parsing)."""
    if not isinstance(value, str):
        raise ValueError("tag value must be a string")
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("tag value is required")
    if len(trimmed) > TAG_VALUE_MAX_LENGTH:
        raise ValueError(f"tag values must be at most {TAG_VALUE_MAX_LENGTH} characters")
    for forbidden in _TAG_VALUE_FORBIDDEN_CHARS:
        if forbidden in trimmed:
            raise ValueError(f"tag values must not contain '{forbidden}'")
    return trimmed


def normalize_optional_tag_value(value: object) -> str | None:
    """Normalize an optional tag value (the delete path's standalone-label id).
    None or blank -> None; otherwise apply the required-value rules."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return normalize_tag_value(value)


# Curated, semantic color names (not freeform hex) so the frontend can guarantee
# legible light/dark renderings by mapping each name to a vetted pair. This list is
# the server-side source of truth; the frontend mirrors it. Order is the random-pick
# pool — keep it stable so colors don't churn across deploys.
TAG_COLOR_PALETTE: tuple[str, ...] = (
    "gray",
    "red",
    "orange",
    "amber",
    "yellow",
    "green",
    "teal",
    "blue",
    "cyan",
    "indigo",
    "purple",
    "pink",
)
_TAG_COLOR_PALETTE_SET = frozenset(TAG_COLOR_PALETTE)


def normalize_tag_color(color: object) -> str:
    """Validate a palette color name against the curated palette. Trims and
    lowercases; raises ValueError for anything outside the palette so freeform
    hex / arbitrary names can't bypass the legible light/dark guarantee."""
    if not isinstance(color, str):
        raise ValueError("tag color must be a string")
    normalized = color.strip().lower()
    if normalized not in _TAG_COLOR_PALETTE_SET:
        raise ValueError(f"tag color must be one of: {', '.join(TAG_COLOR_PALETTE)}")
    return normalized


def random_tag_color() -> str:
    """Pick a palette color for a newly registered (key, value) with no explicit color."""
    return random.choice(TAG_COLOR_PALETTE)


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
