from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from contextvars import ContextVar, Token
from threading import RLock

import structlog

from skyvern.client import SkyvernEnvironment
from skyvern.config import settings
from skyvern.library.skyvern import Skyvern

from .api_key_hash import hash_api_key_for_cache

_skyvern_instance: ContextVar[Skyvern | None] = ContextVar("skyvern_instance", default=None)
_api_key_override: ContextVar[str | None] = ContextVar("skyvern_api_key_override", default=None)
_global_skyvern_instance: Skyvern | None = None
_api_key_clients: OrderedDict[str, Skyvern] = OrderedDict()
_clients_lock = RLock()
LOG = structlog.get_logger(__name__)


def _resolve_api_key_cache_size() -> int:
    raw = os.environ.get("SKYVERN_MCP_API_KEY_CLIENT_CACHE_SIZE", "128")
    try:
        return max(1, int(raw))
    except ValueError:
        return 128


_API_KEY_CLIENT_CACHE_MAX = _resolve_api_key_cache_size()


def _cache_key(api_key: str) -> str:
    """Hash API key so raw secrets are never stored as dict keys."""
    return hash_api_key_for_cache(api_key)


def _resolve_api_key() -> str | None:
    return settings.SKYVERN_API_KEY or os.environ.get("SKYVERN_API_KEY")


def _resolve_base_url() -> str | None:
    return settings.SKYVERN_BASE_URL or os.environ.get("SKYVERN_BASE_URL")


def _build_cloud_client(api_key: str) -> Skyvern:
    return Skyvern(
        api_key=api_key,
        environment=SkyvernEnvironment.CLOUD,
        base_url=_resolve_base_url(),
    )


def _close_skyvern_instance_best_effort(instance: Skyvern) -> None:
    """Close a Skyvern instance, regardless of whether an event loop is running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(instance.aclose())
        except Exception:
            LOG.debug("Failed to close evicted Skyvern client", exc_info=True)
        return

    task = loop.create_task(instance.aclose())

    def _on_done(done: asyncio.Task[None]) -> None:
        try:
            done.result()
        except Exception:
            LOG.debug("Failed to close evicted Skyvern client", exc_info=True)

    task.add_done_callback(_on_done)


def get_active_api_key() -> str | None:
    """Return the effective API key for this request/context."""
    return _api_key_override.get() or _resolve_api_key()


def set_api_key_override(api_key: str | None) -> Token[str | None]:
    """Set request-scoped API key override for MCP HTTP requests."""
    _skyvern_instance.set(None)
    return _api_key_override.set(api_key)


def reset_api_key_override(token: Token[str | None]) -> None:
    """Reset request-scoped API key override."""
    _api_key_override.reset(token)
    _skyvern_instance.set(None)


def get_skyvern() -> Skyvern:
    """Get or create a Skyvern client instance."""
    global _global_skyvern_instance

    override_api_key = _api_key_override.get()
    if override_api_key:
        instance = _skyvern_instance.get()
        if instance is None:
            key = _cache_key(override_api_key)
            evicted_clients: list[Skyvern] = []
            # Hold lock across lookup + build + insert to prevent two coroutines
            # from both building a client for the same API key concurrently.
            with _clients_lock:
                instance = _api_key_clients.get(key)
                if instance is not None:
                    _api_key_clients.move_to_end(key)
                else:
                    instance = _build_cloud_client(override_api_key)
                    _api_key_clients[key] = instance
                    _api_key_clients.move_to_end(key)
                    while len(_api_key_clients) > _API_KEY_CLIENT_CACHE_MAX:
                        _, evicted = _api_key_clients.popitem(last=False)
                        evicted_clients.append(evicted)
            for evicted in evicted_clients:
                _close_skyvern_instance_best_effort(evicted)
        _skyvern_instance.set(instance)
        return instance

    instance = _skyvern_instance.get()
    if instance is None:
        with _clients_lock:
            instance = _global_skyvern_instance
            if instance is None:
                api_key = _resolve_api_key()
                if api_key:
                    instance = _build_cloud_client(api_key)
                else:
                    instance = Skyvern.local()
                _global_skyvern_instance = instance
    _skyvern_instance.set(instance)
    return instance


async def close_skyvern() -> None:
    """Close active Skyvern client(s) and release Playwright resources."""
    global _global_skyvern_instance

    instances: list[Skyvern] = []
    seen: set[int] = set()
    with _clients_lock:
        candidates = (_skyvern_instance.get(), _global_skyvern_instance, *_api_key_clients.values())
        _api_key_clients.clear()
        _global_skyvern_instance = None

    for candidate in candidates:
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        instances.append(candidate)

    for instance in instances:
        try:
            await instance.aclose()
        except Exception:
            LOG.warning("Failed to close Skyvern client", exc_info=True)

    _skyvern_instance.set(None)
