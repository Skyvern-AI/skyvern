VALID_RUN_WITH_VALUES = ("agent", "code")


def normalize_run_with(v: str | None) -> str:
    """Normalize null and legacy 'code_v2' to valid run_with values."""
    if v is None:
        return "agent"
    if v == "code_v2":
        return "code"
    if v not in VALID_RUN_WITH_VALUES:
        raise ValueError(f"run_with must be 'agent' or 'code', got '{v}'")
    return v
