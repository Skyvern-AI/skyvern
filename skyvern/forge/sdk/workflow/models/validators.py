import structlog

LOG = structlog.get_logger()

VALID_RUN_WITH_VALUES = ("agent", "code")
# Known legacy values in prod: 'ai' (-> 'agent'), 'code_v2' (-> 'code')
_LEGACY_RUN_WITH_MAP = {"ai": "agent", "code_v2": "code"}


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
