from __future__ import annotations

import hashlib
import os


def _resolve_api_key_hash_iterations() -> int:
    raw = os.environ.get("SKYVERN_MCP_API_KEY_HASH_ITERATIONS", "120000")
    try:
        return max(10_000, int(raw))
    except ValueError:
        return 120_000


_API_KEY_HASH_ITERATIONS = _resolve_api_key_hash_iterations()
_API_KEY_HASH_SALT = os.environ.get(
    "SKYVERN_MCP_API_KEY_HASH_SALT",
    "skyvern-mcp-api-key-cache-v1",
).encode("utf-8")


def hash_api_key_for_cache(api_key: str) -> str:
    """Derive a deterministic, non-reversible fingerprint for API-key keyed caches."""
    return hashlib.pbkdf2_hmac(
        "sha256",
        api_key.encode("utf-8"),
        _API_KEY_HASH_SALT,
        _API_KEY_HASH_ITERATIONS,
    ).hex()
