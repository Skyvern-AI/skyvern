"""Short-lived worker-to-API handoff for Copilot origin-run secret scrubbing."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.encrypt import encryptor
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.utils.secret_redaction import collect_redactable_secret_values

LOG = structlog.get_logger()

_CACHE_TTL_SECONDS = 600
_CACHE_PREFIX = "copilot:origin-run-runtime-secrets:v1"
_ENCRYPTED_PREFIX = "aes:"
_LOCAL_PREFIX = "local:"
_CLEAR_SENTINEL = "cleared"
_READ_ATTEMPTS = 9
_READ_INTERVAL_SECONDS = 0.25


@dataclass
class _LocalLockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


_LOCAL_LOCKS: dict[tuple[asyncio.AbstractEventLoop, str], _LocalLockEntry] = {}


def _cache_key(organization_id: str, workflow_run_id: str) -> str:
    return f"{_CACHE_PREFIX}:{organization_id}:{workflow_run_id}"


@asynccontextmanager
async def _consume_lock(key: str) -> AsyncIterator[None]:
    local_key = (asyncio.get_running_loop(), key)
    entry = _LOCAL_LOCKS.setdefault(local_key, _LocalLockEntry())
    entry.users += 1
    try:
        async with entry.lock:
            async with app.CACHE.get_lock(f"{key}:consume", blocking_timeout=5, timeout=10):
                yield
    finally:
        entry.users -= 1
        if entry.users == 0 and _LOCAL_LOCKS.get(local_key) is entry:
            _LOCAL_LOCKS.pop(local_key, None)


async def publish_copilot_runtime_secret_values(
    *,
    organization_id: str,
    workflow_run_id: str,
    workflow_run_context: Any,
) -> bool:
    """Publish exact terminal values; shared caches receive ciphertext only."""
    # Static credential values are already bound to the origin-run registry before dispatch.
    # Bridge only values minted at runtime: ``secrets`` also contains routing metadata such as
    # ``totp_identifier``, which is a capability reference and must never become a scrub value.
    values = collect_redactable_secret_values({}, otp_values=workflow_run_context.runtime_otp_values)
    payload = json.dumps(
        {
            "organization_id": organization_id,
            "workflow_run_id": workflow_run_id,
            "values": sorted(values),
        },
        separators=(",", ":"),
    )
    try:
        if app.CACHE.is_shared:
            stored = _ENCRYPTED_PREFIX + await encryptor.encrypt(payload, EncryptMethod.AES)
        else:
            stored = _LOCAL_PREFIX + payload
        await app.CACHE.set(_cache_key(organization_id, workflow_run_id), stored, ex=_CACHE_TTL_SECONDS)
    except Exception:
        LOG.warning(
            "Copilot origin-run runtime secrets could not be handed off",
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
            exc_info=True,
        )
        return False
    return True


async def consume_copilot_runtime_secret_values(
    *,
    organization_id: str,
    workflow_run_id: str,
) -> set[str] | None:
    """Consume a matching handoff once; None means disclosure must remain closed."""
    key = _cache_key(organization_id, workflow_run_id)
    try:
        async with _consume_lock(key):
            stored: Any = None
            for attempt in range(_READ_ATTEMPTS):
                stored = await app.CACHE.get(key)
                if stored is not None:
                    break
                if attempt + 1 < _READ_ATTEMPTS:
                    await asyncio.sleep(_READ_INTERVAL_SECONDS)
            if not isinstance(stored, str) or stored == _CLEAR_SENTINEL:
                return None
            if stored.startswith(_ENCRYPTED_PREFIX):
                payload = await encryptor.decrypt(stored[len(_ENCRYPTED_PREFIX) :], EncryptMethod.AES)
            elif stored.startswith(_LOCAL_PREFIX) and not app.CACHE.is_shared:
                payload = stored[len(_LOCAL_PREFIX) :]
            else:
                return None
            decoded = json.loads(payload)
            if (
                not isinstance(decoded, dict)
                or decoded.get("organization_id") != organization_id
                or decoded.get("workflow_run_id") != workflow_run_id
                or not isinstance(decoded.get("values"), list)
                or any(not isinstance(value, str) for value in decoded["values"])
            ):
                return None
            await app.CACHE.set(key, _CLEAR_SENTINEL, ex=1)
            return set(decoded["values"])
    except Exception:
        LOG.warning(
            "Copilot origin-run runtime secret handoff could not be consumed",
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
            exc_info=True,
        )
        return None
