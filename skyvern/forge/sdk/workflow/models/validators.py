import structlog

LOG = structlog.get_logger()

VALID_RUN_WITH_VALUES = ("agent", "code")
# Known legacy values in prod: 'ai' (-> 'agent'), 'code_v2' (-> 'code')
_LEGACY_RUN_WITH_MAP = {"ai": "agent", "code_v2": "code"}

RUN_METADATA_MAX_KEYS = 20
RUN_METADATA_MAX_KEY_LENGTH = 64
RUN_METADATA_MAX_VALUE_LENGTH = 256


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


def normalize_run_metadata(v: dict[str, str] | None) -> dict[str, str] | None:
    """Normalize and bound user-supplied workflow run metadata tags."""
    if v is None:
        return None

    if len(v) > RUN_METADATA_MAX_KEYS:
        raise ValueError(f"run_metadata can include at most {RUN_METADATA_MAX_KEYS} entries")

    normalized: dict[str, str] = {}
    for key, value in v.items():
        trimmed_key = key.strip()
        trimmed_value = value.strip()
        if not trimmed_key or not trimmed_value:
            continue
        if len(trimmed_key) > RUN_METADATA_MAX_KEY_LENGTH:
            raise ValueError(f"run_metadata keys must be at most {RUN_METADATA_MAX_KEY_LENGTH} characters")
        if len(trimmed_value) > RUN_METADATA_MAX_VALUE_LENGTH:
            raise ValueError(f"run_metadata values must be at most {RUN_METADATA_MAX_VALUE_LENGTH} characters")
        normalized[trimmed_key] = trimmed_value

    return normalized or None
