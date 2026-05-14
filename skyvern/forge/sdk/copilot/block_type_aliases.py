from __future__ import annotations

COPILOT_BLOCK_TYPE_ALIASES: dict[str, str] = {
    "browser_task": "navigation",
}


def normalize_copilot_block_type_alias(value: str) -> str:
    normalized = value.strip().lower()
    return COPILOT_BLOCK_TYPE_ALIASES.get(normalized, normalized)
