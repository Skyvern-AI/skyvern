import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any, Literal

import structlog
from cachetools import TTLCache  # type: ignore[import-untyped]

from skyvern.forge.sdk.core import skyvern_context

LOG = structlog.get_logger()

EXPERIMENTATION_CACHE_TTL = 300  # seconds (5 minutes)
EXPERIMENTATION_CACHE_MAX_SIZE = 100000  # Max entries per cache
FEATURE_FLAG_CACHE_BYPASS_NAMES = frozenset({"RATE_LIMITING_ENABLED"})

ResolutionKind = Literal["enabled", "value", "payload"]


def _serialize_properties(properties: dict | None = None) -> str:
    return json.dumps(properties or {}, sort_keys=True, default=str)


def _make_cache_key(feature_name: str, distinct_id: str, properties: dict | None = None) -> tuple[str, str, str]:
    return feature_name, distinct_id, _serialize_properties(properties)


def should_bypass_feature_flag_cache(feature_name: str) -> bool:
    return feature_name in FEATURE_FLAG_CACHE_BYPASS_NAMES


def _serialize_feature_resolution_value(resolution_kind: ResolutionKind, resolved_value: Any) -> bool | str | None:
    if resolved_value is None:
        return None
    if isinstance(resolved_value, bool):
        return resolved_value
    if isinstance(resolved_value, str):
        return resolved_value
    if isinstance(resolved_value, (int, float)):
        return str(resolved_value)
    if resolution_kind == "payload":
        return json.dumps(resolved_value, sort_keys=True, default=str)
    return str(resolved_value)


def _should_record_feature_flags(context: skyvern_context.SkyvernContext) -> bool:
    return bool(context.workflow_run_id or context.task_id or context.task_v2_id or context.run_id)


def _record_feature_flag_entry(
    context: skyvern_context.SkyvernContext | None,
    *,
    feature_name: str,
    resolved_value: bool | str | None,
) -> None:
    if context is None:
        return
    if not _should_record_feature_flags(context):
        return

    context.feature_flag_entries[feature_name] = resolved_value


def _build_feature_flag_resolution_log_fields(
    context: skyvern_context.SkyvernContext | None,
    *,
    feature_name: str,
    resolution_kind: ResolutionKind,
    resolved_value: Any,
) -> dict[str, Any]:
    log_fields: dict[str, Any] = {
        "feature_name": feature_name,
        "resolution_kind": resolution_kind,
        "resolved_value": resolved_value,
    }
    if context is None:
        return log_fields

    if context.organization_id:
        log_fields["organization_id"] = context.organization_id
    if context.request_id:
        log_fields["request_id"] = context.request_id
    if context.task_id:
        log_fields["task_id"] = context.task_id
    if context.task_v2_id:
        log_fields["task_v2_id"] = context.task_v2_id
    if context.workflow_run_id:
        log_fields["workflow_run_id"] = context.workflow_run_id
    if context.workflow_permanent_id:
        log_fields["workflow_permanent_id"] = context.workflow_permanent_id
    if context.browser_session_id:
        log_fields["browser_session_id"] = context.browser_session_id
    return log_fields


def flush_feature_flags(context: skyvern_context.SkyvernContext | None = None) -> None:
    resolved_context = context or skyvern_context.current()
    if resolved_context is not None:
        resolved_context.flush_feature_flags()


def record_feature_flag_resolution(
    *,
    feature_name: str,
    resolution_kind: ResolutionKind,
    resolved_value: Any,
) -> None:
    # Callers must skyvern_context.set(...) with the run/task ID set BEFORE evaluating
    # per-run flags — _record_feature_flag_entry short-circuits on contexts missing
    # workflow_run_id/task_id/task_v2_id/run_id, and the resolution would be lost.
    context = skyvern_context.current()
    serialized_resolved_value = _serialize_feature_resolution_value(resolution_kind, resolved_value)
    LOG.debug(
        "feature_flag_resolution",
        **_build_feature_flag_resolution_log_fields(
            context,
            feature_name=feature_name,
            resolution_kind=resolution_kind,
            resolved_value=resolved_value,
        ),
    )
    _record_feature_flag_entry(
        context,
        feature_name=feature_name,
        resolved_value=serialized_resolved_value,
    )


class DataHashFreshnessCache:
    """TTL gate for checking whether locally loaded feature flag data is fresh."""

    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = monotonic) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._last_checked_at: float | None = None
        self._lock = asyncio.Lock()

    def _is_fresh(self) -> bool:
        if self._last_checked_at is None:
            return False
        return self._clock() - self._last_checked_at < self._ttl_seconds

    async def refresh_if_stale(self, refresh: Callable[[], Awaitable[None]], *, bypass_cache: bool = False) -> None:
        """Refresh on cold misses, but serve loaded data while a stale refresh is already in flight."""

        if bypass_cache or self._ttl_seconds <= 0:
            async with self._lock:
                await refresh()
                self._last_checked_at = self._clock()
            return

        if self._is_fresh():
            return

        if self._last_checked_at is not None and self._lock.locked():
            return

        async with self._lock:
            if self._is_fresh():
                return
            await refresh()
            self._last_checked_at = self._clock()


class BaseExperimentationProvider(ABC):
    def __init__(self) -> None:
        self.result_map: TTLCache = TTLCache(maxsize=EXPERIMENTATION_CACHE_MAX_SIZE, ttl=EXPERIMENTATION_CACHE_TTL)
        self.variant_map: TTLCache = TTLCache(maxsize=EXPERIMENTATION_CACHE_MAX_SIZE, ttl=EXPERIMENTATION_CACHE_TTL)
        self.payload_map: TTLCache = TTLCache(maxsize=EXPERIMENTATION_CACHE_MAX_SIZE, ttl=EXPERIMENTATION_CACHE_TTL)
        self.tri_state_map: TTLCache = TTLCache(maxsize=EXPERIMENTATION_CACHE_MAX_SIZE, ttl=EXPERIMENTATION_CACHE_TTL)

    @abstractmethod
    async def _is_feature_enabled(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        """Check if a specific feature is enabled."""

    async def _prepare_feature_flag_resolution(self, feature_name: str, *, cached: bool) -> None:
        return None

    async def is_feature_enabled(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        await self._prepare_feature_flag_resolution(feature_name, cached=False)
        feature_flag_value = await self._is_feature_enabled(feature_name, distinct_id, properties)
        record_feature_flag_resolution(
            feature_name=feature_name,
            resolution_kind="enabled",
            resolved_value=feature_flag_value,
        )
        return feature_flag_value

    async def is_feature_enabled_cached(
        self, feature_name: str, distinct_id: str, properties: dict | None = None
    ) -> bool:
        cache_key = _make_cache_key(feature_name, distinct_id, properties)
        if should_bypass_feature_flag_cache(feature_name):
            await self._prepare_feature_flag_resolution(feature_name, cached=False)
            feature_flag_value = await self._is_feature_enabled(feature_name, distinct_id, properties)
        elif cache_key in self.result_map:
            feature_flag_value = self.result_map[cache_key]
        else:
            await self._prepare_feature_flag_resolution(feature_name, cached=True)
            feature_flag_value = await self._is_feature_enabled(feature_name, distinct_id, properties)
            self.result_map[cache_key] = feature_flag_value
        record_feature_flag_resolution(
            feature_name=feature_name,
            resolution_kind="enabled",
            resolved_value=feature_flag_value,
        )
        return feature_flag_value

    async def _resolve_feature_flag(
        self, feature_name: str, distinct_id: str, properties: dict | None = None
    ) -> bool | None:
        """Tri-state flag resolution: True/False, or None when the flag is undefined/unknown.

        Base implementation collapses to the boolean resolver; providers that can tell an
        undefined flag apart from an explicit ``False`` override this to surface ``None``.
        """
        return await self._is_feature_enabled(feature_name, distinct_id, properties)

    async def resolve_feature_flag_cached(
        self, feature_name: str, distinct_id: str, properties: dict | None = None
    ) -> bool | None:
        cache_key = _make_cache_key(feature_name, distinct_id, properties)
        if should_bypass_feature_flag_cache(feature_name):
            await self._prepare_feature_flag_resolution(feature_name, cached=False)
            resolved = await self._resolve_feature_flag(feature_name, distinct_id, properties)
        elif cache_key in self.tri_state_map:
            resolved = self.tri_state_map[cache_key]
        else:
            await self._prepare_feature_flag_resolution(feature_name, cached=True)
            resolved = await self._resolve_feature_flag(feature_name, distinct_id, properties)
            self.tri_state_map[cache_key] = resolved
        record_feature_flag_resolution(
            feature_name=feature_name,
            resolution_kind="enabled",
            resolved_value=resolved,
        )
        return resolved

    @abstractmethod
    async def _get_value(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        """Get the value of a feature."""

    async def get_value(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        await self._prepare_feature_flag_resolution(feature_name, cached=False)
        variant = await self._get_value(feature_name, distinct_id, properties)
        record_feature_flag_resolution(
            feature_name=feature_name,
            resolution_kind="value",
            resolved_value=variant,
        )
        return variant

    @abstractmethod
    async def _get_payload(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> Any:
        """Get the payload for a feature flag if it exists."""

    async def get_payload(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> Any:
        await self._prepare_feature_flag_resolution(feature_name, cached=False)
        payload = await self._get_payload(feature_name, distinct_id, properties)
        record_feature_flag_resolution(
            feature_name=feature_name,
            resolution_kind="payload",
            resolved_value=payload,
        )
        return payload

    async def get_value_cached(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        cache_key = _make_cache_key(feature_name, distinct_id, properties)
        if cache_key in self.variant_map:
            variant = self.variant_map[cache_key]
        else:
            await self._prepare_feature_flag_resolution(feature_name, cached=True)
            variant = await self._get_value(feature_name, distinct_id, properties)
            self.variant_map[cache_key] = variant
        record_feature_flag_resolution(
            feature_name=feature_name,
            resolution_kind="value",
            resolved_value=variant,
        )
        return variant

    async def get_payload_cached(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> Any:
        cache_key = _make_cache_key(feature_name, distinct_id, properties)
        if cache_key in self.payload_map:
            payload = self.payload_map[cache_key]
        else:
            await self._prepare_feature_flag_resolution(feature_name, cached=True)
            payload = await self._get_payload(feature_name, distinct_id, properties)
            self.payload_map[cache_key] = payload
        record_feature_flag_resolution(
            feature_name=feature_name,
            resolution_kind="payload",
            resolved_value=payload,
        )
        return payload


class NoOpExperimentationProvider(BaseExperimentationProvider):
    def __init__(self) -> None:
        super().__init__()

    async def _is_feature_enabled(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        return False

    async def _get_value(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        return None

    async def _get_payload(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> Any:
        return None
