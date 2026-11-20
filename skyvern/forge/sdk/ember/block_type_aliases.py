from __future__ import annotations

EMBER_BLOCK_TYPE_ALIASES: dict[str, str] = {
    "browser_task": "navigation",
}


def normalize_ember_block_type_alias(value: str) -> str:
    normalized = value.strip().lower()
    return EMBER_BLOCK_TYPE_ALIASES.get(normalized, normalized)
