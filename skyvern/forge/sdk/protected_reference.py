"""Consumer-bound resolution contract for browser-firewall protected references."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn, Protocol

from skyvern.forge.sdk.browser_action_policy import ProtectedReference, ProtectedReferenceKind

ProtectedValueResolver = Callable[[], Awaitable[str]]


class ProtectedReferenceErrorReason(StrEnum):
    INCOMPLETE_BINDING = "incomplete_binding"
    NOT_AUTHORIZED = "not_authorized"
    RESOLUTION_FAILED = "resolution_failed"


class ProtectedReferenceError(RuntimeError):
    """A safe failure containing no protected identifiers or values."""

    def __init__(self, reason: ProtectedReferenceErrorReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


def _raise_safe_error(reason: ProtectedReferenceErrorReason) -> NoReturn:
    raise ProtectedReferenceError(reason)


class ProtectedReferenceResolver(Protocol):
    """Capability exposed only to final trusted sinks."""

    async def resolve(self, ref: ProtectedReference, run_id: str, consumer_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class _BindingKey:
    kind: ProtectedReferenceKind
    reference_id: str
    owner_id: str
    run_id: str
    consumer_id: str


def _present(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _sanitize_control_error(error: BaseException) -> BaseException:
    if isinstance(error, BaseExceptionGroup):
        children = [_sanitize_control_error(child) for child in error.exceptions]
        return BaseExceptionGroup("protected resolver interrupted", children)
    if isinstance(error, asyncio.CancelledError):
        return asyncio.CancelledError()
    if isinstance(error, KeyboardInterrupt):
        return KeyboardInterrupt()
    if isinstance(error, SystemExit):
        code = error.code
        return SystemExit(code if type(code) is int else int(code is not None))
    if isinstance(error, GeneratorExit):
        return GeneratorExit()
    if isinstance(error, Exception):
        return Exception()
    return BaseException()


async def _resolve_bound_value(resolver: ProtectedValueResolver) -> str:
    control_error: BaseException | None = None
    try:
        resolved = await resolver()
    except Exception:
        pass
    except BaseException as error:
        control_error = _sanitize_control_error(error)
    else:
        if isinstance(resolved, str) and resolved:
            return resolved
        del resolved

    del resolver
    if control_error is not None:
        safe_error = control_error
        del control_error
        raise safe_error
    del control_error
    # Raise after cleanup so provider exceptions and rejected values cannot survive in artifacts.
    _raise_safe_error(ProtectedReferenceErrorReason.RESOLUTION_FAILED)


class ProtectedReferenceStore:
    """Binds opaque capabilities to deferred resolvers without retaining protected values."""

    def __init__(self) -> None:
        self._bindings: dict[_BindingKey, ProtectedValueResolver] = {}
        self._issued_reference_ids: set[str] = set()

    def bind(
        self,
        *,
        kind: ProtectedReferenceKind,
        owner_id: str,
        run_id: str,
        consumer_id: str,
        resolver: ProtectedValueResolver,
    ) -> ProtectedReference:
        if (
            not isinstance(kind, ProtectedReferenceKind)
            or not _present(owner_id)
            or not _present(run_id)
            or not _present(consumer_id)
            or not callable(resolver)
        ):
            del self, kind, owner_id, run_id, consumer_id, resolver
            _raise_safe_error(ProtectedReferenceErrorReason.INCOMPLETE_BINDING)

        reference_id = self._new_reference_id()
        ref = ProtectedReference(kind=kind, reference_id=reference_id, owner_id=owner_id)
        key = _BindingKey(kind, reference_id, owner_id, run_id, consumer_id)
        self._bindings[key] = resolver
        return ref

    async def resolve(self, ref: ProtectedReference, run_id: str, consumer_id: str) -> str:
        if (
            not isinstance(ref, ProtectedReference)
            or not ref.complete
            or not _present(ref.reference_id)
            or not _present(ref.owner_id)
            or not _present(run_id)
            or not _present(consumer_id)
        ):
            del self, ref, run_id, consumer_id
            _raise_safe_error(ProtectedReferenceErrorReason.INCOMPLETE_BINDING)

        key = _BindingKey(ref.kind, ref.reference_id, ref.owner_id, run_id, consumer_id)
        resolver = self._bindings.get(key)
        if resolver is None:
            del self, ref, run_id, consumer_id, key, resolver
            _raise_safe_error(ProtectedReferenceErrorReason.NOT_AUTHORIZED)

        resolution = _resolve_bound_value(resolver)
        del self, ref, run_id, consumer_id, key, resolver
        return await resolution

    def _new_reference_id(self) -> str:
        while True:
            reference_id = f"pref_{secrets.token_urlsafe(24)}"
            if reference_id not in self._issued_reference_ids:
                self._issued_reference_ids.add(reference_id)
                return reference_id
