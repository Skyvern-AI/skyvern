from __future__ import annotations

import hashlib
import hmac
import secrets

# A random key per process is safe only because fingerprints key in-process maps and are never persisted, logged or
# compared across processes.
_API_KEY_HASH_KEY = secrets.token_bytes(32)


def hash_api_key_for_cache(api_key: str) -> str:
    """Derive a non-reversible fingerprint for API-key keyed caches, stable for the life of this process."""
    return hmac.new(_API_KEY_HASH_KEY, api_key.encode("utf-8"), hashlib.sha256).hexdigest()
