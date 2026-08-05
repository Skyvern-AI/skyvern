"""One-shot PREVIEW-to-COMMIT bindings for browser effects (SKY-12880)."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
import secrets
import string
import traceback
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Generic, TypeVar, cast, final
from urllib.parse import urlsplit

from skyvern.forge.sdk.browser_action_policy import ProtectedReference, canonicalize_origin
from skyvern.forge.sdk.protected_reference import ProtectedReferenceResolver

T = TypeVar("T")

_BROWSER_NETWORK_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_DEFAULT_NETWORK_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}
_PATH_CHARACTERS = frozenset(string.ascii_letters + string.digits + "-._~!$&'()*+,;=:@/")
# HTTP(S) and WebSocket URLs use WHATWG's special-query encode set, which also rewrites apostrophes.
_QUERY_CHARACTERS = (_PATH_CHARACTERS - frozenset("'")) | frozenset("?[]")
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_HTTP_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_FETCH_NORMALIZED_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "POST", "PUT"})
_MAX_JSON_DEPTH = 64


@dataclass(frozen=True, slots=True)
class ApprovalId:
    """Opaque handle to one pending approval; never a post-consumption capability."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or not self.value:
            raise TypeError("Approval IDs must be non-empty strings")


class ApprovalMode(StrEnum):
    OBSERVE = "observe"
    ENFORCE = "enforce"


class ApprovalReason(StrEnum):
    FRESH_APPROVAL_REQUIRED = "fresh_approval_required"
    APPROVAL_REPLAYED = "approval_replayed"
    RUN_IDENTITY_MISMATCH = "run_identity_mismatch"
    ACTION_NONCE_MISMATCH = "action_nonce_mismatch"
    SINK_SEQUENCE_MISMATCH = "sink_sequence_mismatch"
    OBSERVATION_EPOCH_MISMATCH = "observation_epoch_mismatch"
    PAGE_IDENTITY_MISMATCH = "page_identity_mismatch"
    TAB_IDENTITY_MISMATCH = "tab_identity_mismatch"
    FRAME_IDENTITY_MISMATCH = "frame_identity_mismatch"
    SINK_KIND_MISMATCH = "sink_kind_mismatch"
    CANONICAL_TARGET_MISMATCH = "canonical_target_mismatch"
    CANONICAL_METHOD_MISMATCH = "canonical_method_mismatch"
    NON_SECRET_ARGS_MISMATCH = "non_secret_args_mismatch"
    PROTECTED_REFERENCE_IDS_MISMATCH = "protected_reference_ids_mismatch"
    PROTECTED_REFERENCE_SLOTS_MISMATCH = "protected_reference_slots_mismatch"
    CANONICAL_TARGET_UNSUPPORTED = "canonical_target_unsupported"
    CONSUMER_MISMATCH = "consumer_mismatch"


@dataclass(frozen=True, slots=True)
class FrozenEffectDescriptor:
    """Immutable authority for exactly one low-level effect.

    ``non_secret_args`` contains sorted ``(name, canonical_value)`` string pairs. Protected values
    never enter this object; only their opaque reference IDs do.
    """

    run_identity: str
    action_nonce: str
    sink_sequence: int
    observation_epoch: int
    page_identity: str
    tab_identity: str
    frame_identity: str
    sink_kind: str
    canonical_target: str
    canonical_method: str
    non_secret_args: tuple[tuple[str, str], ...] = ()
    protected_reference_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        identity_values = (
            self.run_identity,
            self.action_nonce,
            self.page_identity,
            self.tab_identity,
            self.frame_identity,
            self.sink_kind,
            self.canonical_target,
            self.canonical_method,
        )
        if any(type(value) is not str or not value for value in identity_values):
            raise TypeError("Effect descriptor identity, sink, target, and method fields must be non-empty strings")
        if any(type(value) is not int or value < 0 for value in (self.sink_sequence, self.observation_epoch)):
            raise ValueError("Effect descriptor sequence and epoch must be non-negative integers")
        if (
            not isinstance(self.non_secret_args, tuple)
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or type(item[0]) is not str
                or not item[0]
                or type(item[1]) is not str
                for item in self.non_secret_args
            )
            or tuple(sorted(self.non_secret_args)) != self.non_secret_args
            or len(dict(self.non_secret_args)) != len(self.non_secret_args)
        ):
            raise TypeError("non_secret_args must be immutable tuple[str, str] pairs with unique canonical keys")
        if (
            not isinstance(self.protected_reference_ids, tuple)
            or any(type(reference_id) is not str or not reference_id for reference_id in self.protected_reference_ids)
            or len(set(self.protected_reference_ids)) != len(self.protected_reference_ids)
        ):
            raise ValueError("protected_reference_ids must be unique non-empty strings in resolution order")


@dataclass(frozen=True, slots=True)
class EffectDescriptorLineage:
    """Identity allocated before PREVIEW and reused only for its matching COMMIT."""

    run_identity: str
    action_nonce: str
    sink_sequence: int


def canonicalize_effect_target(target: str) -> str:
    """Return the shared exact browser-wire form, or reject targets Chromium may rewrite.

    This deliberately implements a restricted enrollment subset instead of attempting to reproduce
    the full WHATWG URL serializer. Callers and network monitors must import this same function.
    """

    if type(target) is not str or not target or target != target.strip() or not target.isascii():
        raise EffectApprovalRejected(ApprovalReason.CANONICAL_TARGET_UNSUPPORTED)
    origin = canonicalize_origin(target)
    if origin is None:
        raise EffectApprovalRejected(ApprovalReason.CANONICAL_TARGET_UNSUPPORTED)
    parsed = urlsplit(target)
    if parsed.hostname is None or parsed.hostname.endswith("."):
        raise EffectApprovalRejected(ApprovalReason.CANONICAL_TARGET_UNSUPPORTED)
    if not _is_browser_wire_host(parsed.netloc, origin.host):
        raise EffectApprovalRejected(ApprovalReason.CANONICAL_TARGET_UNSUPPORTED)
    raw_scheme = parsed.scheme.lower()
    if raw_scheme not in _BROWSER_NETWORK_SCHEMES:
        raise EffectApprovalRejected(ApprovalReason.CANONICAL_TARGET_UNSUPPORTED)
    path = parsed.path or "/"
    _validate_browser_wire_component(path, _PATH_CHARACTERS)
    _validate_browser_wire_component(parsed.query, _QUERY_CHARACTERS)
    if any(_DOT_ESCAPE.sub(".", segment).lower() in {".", ".."} for segment in path.split("/")):
        raise EffectApprovalRejected(ApprovalReason.CANONICAL_TARGET_UNSUPPORTED)

    host = f"[{origin.host}]" if ":" in origin.host else origin.host
    port = "" if origin.port == _DEFAULT_NETWORK_PORTS[raw_scheme] else f":{origin.port}"
    before_fragment = target.partition("#")[0]
    query = f"?{parsed.query}" if "?" in before_fragment else ""
    return f"{raw_scheme}://{host}{port}{path}{query}"


_DOT_ESCAPE = re.compile(r"%2e", re.IGNORECASE)


def _is_browser_wire_host(netloc: str, host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return (
            "[" not in netloc
            and "]" not in netloc
            and len(host) <= 253
            and all(_DNS_LABEL.fullmatch(label) for label in host.split("."))
        )
    return address.version == 4 or (netloc.startswith("[") and "." not in host)


def _validate_browser_wire_component(value: str, allowed: frozenset[str]) -> None:
    index = 0
    while index < len(value):
        character = value[index]
        if character == "%":
            if len(value) - index < 3 or any(digit not in string.hexdigits for digit in value[index + 1 : index + 3]):
                raise EffectApprovalRejected(ApprovalReason.CANONICAL_TARGET_UNSUPPORTED)
            index += 3
            continue
        if character not in allowed:
            raise EffectApprovalRejected(ApprovalReason.CANONICAL_TARGET_UNSUPPORTED)
        index += 1


def canonicalize_effect_method(method: str) -> str:
    if type(method) is not str or not _HTTP_TOKEN.fullmatch(method):
        raise ValueError("Effect method must be a non-empty token")
    uppercase = method.upper()
    return uppercase if uppercase in _FETCH_NORMALIZED_METHODS else method


def canonicalize_non_secret_args(args: Mapping[str, object] | None) -> tuple[tuple[str, str], ...]:
    """Freeze public JSON control data; secret-bearing values must use references instead.

    Allowed values are non-sensitive JSON scalars, lists, and maps needed to identify the effect.
    Credentials, verification codes, file contents, signed URLs, and resolved protected values must
    never enter this mapping.
    """

    if args is None:
        return ()
    if not isinstance(args, Mapping):
        raise ValueError("Non-secret arguments must be a mapping")
    if any(type(name) is not str or not name for name in args):
        raise ValueError("Non-secret argument names must be non-empty strings")
    try:
        seen_containers: set[int] = set()
        for value in args.values():
            _validate_json_value(value, seen_containers, depth=0)
        return tuple(
            (name, json.dumps(args[name], allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
            for name in sorted(args)
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Non-secret arguments must contain only JSON values") from error


def _validate_json_value(value: object, seen_containers: set[int], *, depth: int) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("JSON values exceed the supported nesting depth")
    value_type = type(value)
    if value is None or value_type in {bool, int, str}:
        return
    if value_type is float:
        if not math.isfinite(cast(float, value)):
            raise ValueError("Non-finite numbers are not JSON values")
        return
    if value_type not in {list, dict}:
        raise TypeError("Only exact JSON runtime types are supported")

    container_id = id(value)
    if container_id in seen_containers:
        raise ValueError("Cyclic and shared-reference JSON values are not supported")
    seen_containers.add(container_id)
    if value_type is list:
        for item in cast(list[object], value):
            _validate_json_value(item, seen_containers, depth=depth + 1)
    else:
        mapping = cast(dict[object, object], value)
        if any(type(key) is not str for key in mapping):
            raise TypeError("JSON object keys must be strings")
        for item in mapping.values():
            _validate_json_value(item, seen_containers, depth=depth + 1)


class _ObjectIdentityRegistry:
    def __init__(self) -> None:
        self._records: dict[int, tuple[weakref.ReferenceType[object], str]] = {}

    def identify(self, value: object) -> str:
        key = id(value)
        current = self._records.get(key)
        if current is not None and current[0]() is value:
            return current[1]

        def discard(reference: weakref.ReferenceType[object]) -> None:
            current = self._records.get(key)
            if current is not None and current[0] is reference:
                self._records.pop(key, None)

        try:
            reference = weakref.ref(value, discard)
        except TypeError as error:
            raise ValueError("Browser effect identities must be weak-referenceable objects") from error
        identity = secrets.token_urlsafe(24)
        self._records[key] = (reference, identity)
        return identity


class EffectDescriptorFactory:
    """Run-scoped producer for byte-identical PREVIEW and COMMIT descriptors.

    The run identity is the resolver-authorized run ID. ``issue_lineage`` allocates the action nonce
    and sink sequence before PREVIEW, independently of the post-dispatch action ID. Callers supply
    the current accepted scrape epoch plus the live page, tab, and frame objects to ``describe``.
    Sink adapters own their stable namespaced ``sink_kind`` constants.
    """

    def __init__(self, run_identity: str) -> None:
        if type(run_identity) is not str or not run_identity:
            raise ValueError("A descriptor factory requires the resolver-authorized run ID")
        self._run_identity = run_identity
        self._next_sequence = 0
        self._issued: set[EffectDescriptorLineage] = set()
        self._pages, self._tabs, self._frames = (_ObjectIdentityRegistry() for _ in range(3))

    def issue_lineage(self) -> EffectDescriptorLineage:
        lineage = EffectDescriptorLineage(self._run_identity, secrets.token_urlsafe(32), self._next_sequence)
        self._next_sequence += 1
        self._issued.add(lineage)
        return lineage

    def describe(
        self,
        lineage: EffectDescriptorLineage,
        *,
        observation_epoch: int,
        page: object,
        tab: object,
        frame: object,
        sink_kind: str,
        target: str,
        method: str,
        non_secret_args: Mapping[str, object] | None = None,
        protected_references: tuple[ProtectedReference, ...] = (),
    ) -> FrozenEffectDescriptor:
        """Describe one effect twice: once at PREVIEW and again from live values at COMMIT."""

        if lineage not in self._issued:
            raise ValueError("Descriptor lineage was not issued by this run factory")
        if not isinstance(protected_references, tuple) or any(
            type(reference) is not ProtectedReference or not reference.complete for reference in protected_references
        ):
            raise ValueError("Descriptor references must be complete canonical protected references")
        return FrozenEffectDescriptor(
            lineage.run_identity,
            lineage.action_nonce,
            lineage.sink_sequence,
            observation_epoch,
            self._pages.identify(page),
            self._tabs.identify(tab),
            self._frames.identify(frame),
            sink_kind,
            canonicalize_effect_target(target),
            canonicalize_effect_method(method),
            canonicalize_non_secret_args(non_secret_args),
            tuple(reference.reference_id for reference in protected_references),
        )


_CONSUMED_EFFECT_BRAND = object()


@final
class ConsumedEffect:
    """Opaque in-dispatch capability accepted by the causal network monitor.

    There is deliberately no public constructor. Only ``consume_and_dispatch`` brands an active
    instance, and it invalidates the capability before returning to its caller. The nominal type and
    private brand prevent accidental structural satisfaction and fabrication by code without module
    import access. Arbitrary in-process Python is outside this boundary and can bypass Python privacy.
    """

    __slots__ = ("_active", "_brand", "_consumption_id")

    _active: bool
    _brand: object
    _consumption_id: str

    def __new__(cls) -> ConsumedEffect:
        raise TypeError("ConsumedEffect can only be created by consume_and_dispatch")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ConsumedEffect is immutable")

    @property
    def consumption_id(self) -> str:
        if getattr(self, "_brand", None) is not _CONSUMED_EFFECT_BRAND or not getattr(self, "_active", False):
            raise RuntimeError("ConsumedEffect is no longer active or not an active consumed effect")
        return self._consumption_id


@dataclass(frozen=True, slots=True, eq=False)
class TrustedEffectDispatcher(Generic[T]):
    """Final trusted sink bound at PREVIEW and invoked only by COMMIT.

    The callback receives the short-lived causal capability followed by resolved values in the
    same order as ``protected_references``. Value ``i`` is exclusively bound to both
    ``protected_references[i]`` and ``protected_reference_slots[i]``. It must close the causal epoch
    and consume protected values before returning; neither may escape into its result, logs,
    artifacts, or exceptions. The wrapper clears completed sink-frame locals, but preserves raised
    exception objects; dispatchers must never interpolate resolved values into exception messages.
    """

    consumer_id: str
    protected_references: tuple[ProtectedReference, ...]
    dispatch: Callable[[ConsumedEffect, FrozenEffectDescriptor, tuple[str, ...]], Awaitable[T]]
    protected_reference_slots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.consumer_id) is not str
            or not self.consumer_id
            or not isinstance(self.protected_references, tuple)
            or any(
                type(reference) is not ProtectedReference or not reference.complete
                for reference in self.protected_references
            )
            or not callable(self.dispatch)
        ):
            raise TypeError("A trusted effect dispatcher requires an ID, immutable references, and a callback")
        if (
            not isinstance(self.protected_reference_slots, tuple)
            or len(self.protected_reference_slots) != len(self.protected_references)
            or any(type(slot) is not str or not slot for slot in self.protected_reference_slots)
            or len(set(self.protected_reference_slots)) != len(self.protected_reference_slots)
        ):
            raise TypeError("Protected reference slots must be unique non-empty strings parallel to references")


@dataclass(frozen=True, slots=True)
class ApprovalBindingFailure:
    reason: ApprovalReason


class EffectApprovalRejected(RuntimeError):
    def __init__(self, reason: ApprovalReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class _ApprovalRecord:
    descriptor: FrozenEffectDescriptor
    dispatcher: TrustedEffectDispatcher[object]
    consumer_id: str
    dispatch: Callable[[ConsumedEffect, FrozenEffectDescriptor, tuple[str, ...]], Awaitable[object]]
    protected_references: tuple[ProtectedReference, ...]
    protected_reference_slots: tuple[str, ...]


_DESCRIPTOR_FIELDS: tuple[tuple[str, ApprovalReason], ...] = (
    ("run_identity", ApprovalReason.RUN_IDENTITY_MISMATCH),
    ("action_nonce", ApprovalReason.ACTION_NONCE_MISMATCH),
    ("sink_sequence", ApprovalReason.SINK_SEQUENCE_MISMATCH),
    ("observation_epoch", ApprovalReason.OBSERVATION_EPOCH_MISMATCH),
    ("page_identity", ApprovalReason.PAGE_IDENTITY_MISMATCH),
    ("tab_identity", ApprovalReason.TAB_IDENTITY_MISMATCH),
    ("frame_identity", ApprovalReason.FRAME_IDENTITY_MISMATCH),
    ("sink_kind", ApprovalReason.SINK_KIND_MISMATCH),
    ("canonical_target", ApprovalReason.CANONICAL_TARGET_MISMATCH),
    ("canonical_method", ApprovalReason.CANONICAL_METHOD_MISMATCH),
    ("non_secret_args", ApprovalReason.NON_SECRET_ARGS_MISMATCH),
    ("protected_reference_ids", ApprovalReason.PROTECTED_REFERENCE_IDS_MISMATCH),
)


def _mint_consumed_effect() -> ConsumedEffect:
    consumed = object.__new__(ConsumedEffect)
    object.__setattr__(consumed, "_active", True)
    object.__setattr__(consumed, "_brand", _CONSUMED_EFFECT_BRAND)
    object.__setattr__(consumed, "_consumption_id", secrets.token_urlsafe(32))
    return consumed


def _clear_exception_frames(error: BaseException, seen: set[int] | None = None) -> None:
    """Clear completed frames throughout an exception graph without rewriting the errors."""

    if seen is None:
        seen = set()
    error_id = id(error)
    if error_id in seen:
        return
    seen.add(error_id)
    traceback.clear_frames(error.__traceback__)
    if error.__cause__ is not None:
        _clear_exception_frames(error.__cause__, seen)
    if error.__context__ is not None:
        _clear_exception_frames(error.__context__, seen)
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            _clear_exception_frames(nested, seen)


class EffectApprovalStore:
    """Run-scoped in-memory store for one-shot effect approvals.

    Authority inputs are copied at PREVIEW and COMMIT so compare, resolution, and dispatch use one
    value snapshot across awaits. This closes caller mutation windows; arbitrary in-process frame
    access remains outside the capability boundary documented by ``ConsumedEffect``.
    """

    def __init__(self, *, mode: ApprovalMode, resolver: ProtectedReferenceResolver) -> None:
        if not isinstance(mode, ApprovalMode):
            raise TypeError("Effect approval mode must be explicit")
        self._mode = mode
        self._resolver = resolver
        self._pending: dict[ApprovalId, _ApprovalRecord] = {}
        self._consumed: set[ApprovalId] = set()
        self._locks: dict[ApprovalId, asyncio.Lock] = {}
        self._binding_failures: list[ApprovalBindingFailure] = []

    @property
    def binding_failures(self) -> tuple[ApprovalBindingFailure, ...]:
        return tuple(self._binding_failures)

    def preview(
        self,
        descriptor: FrozenEffectDescriptor,
        dispatcher: TrustedEffectDispatcher[T],
    ) -> ApprovalId:
        consumer_id = dispatcher.consumer_id
        dispatch = dispatcher.dispatch
        protected_references = tuple(replace(reference) for reference in dispatcher.protected_references)
        protected_reference_slots = dispatcher.protected_reference_slots
        reference_ids = tuple(reference.reference_id for reference in protected_references)
        if reference_ids != descriptor.protected_reference_ids:
            raise EffectApprovalRejected(ApprovalReason.PROTECTED_REFERENCE_IDS_MISMATCH)
        while True:
            approval_id = ApprovalId(secrets.token_urlsafe(32))
            if approval_id not in self._pending and approval_id not in self._consumed:
                break
        self._pending[approval_id] = _ApprovalRecord(
            replace(descriptor),
            cast(TrustedEffectDispatcher[object], dispatcher),
            consumer_id,
            cast(Callable[[ConsumedEffect, FrozenEffectDescriptor, tuple[str, ...]], Awaitable[object]], dispatch),
            protected_references,
            protected_reference_slots,
        )
        self._locks[approval_id] = asyncio.Lock()
        return approval_id

    async def consume_and_dispatch(
        self,
        approval_id: ApprovalId,
        live_descriptor: FrozenEffectDescriptor,
        dispatcher: TrustedEffectDispatcher[T],
    ) -> T:
        # Unknown IDs never gain authority, so they need no persistent lock. Previewed IDs retain
        # their shared lock to keep compare, consume, and dispatch atomic across concurrent callers.
        lock = self._locks.get(approval_id) or asyncio.Lock()
        async with lock:
            commit_descriptor = replace(live_descriptor)
            consumer_id = dispatcher.consumer_id
            dispatch = dispatcher.dispatch
            protected_references = tuple(replace(reference) for reference in dispatcher.protected_references)
            protected_reference_slots = dispatcher.protected_reference_slots
            reason: ApprovalReason | None
            if approval_id in self._consumed:
                reason = ApprovalReason.APPROVAL_REPLAYED
            else:
                record = self._pending.pop(approval_id, None)
                if record is None:
                    reason = ApprovalReason.FRESH_APPROVAL_REQUIRED
                else:
                    self._consumed.add(approval_id)
                    reason = next(
                        (
                            mismatch
                            for field, mismatch in _DESCRIPTOR_FIELDS
                            if getattr(record.descriptor, field) != getattr(commit_descriptor, field)
                        ),
                        None,
                    )
                    if reason is None and record.dispatcher is not dispatcher:
                        reason = ApprovalReason.CONSUMER_MISMATCH
                    if reason is None and record.consumer_id != consumer_id:
                        reason = ApprovalReason.CONSUMER_MISMATCH
                    if reason is None and record.dispatch is not dispatch:
                        reason = ApprovalReason.CONSUMER_MISMATCH
                    if reason is None and record.protected_references != protected_references:
                        reason = ApprovalReason.CONSUMER_MISMATCH
                    if reason is None and record.protected_reference_slots != protected_reference_slots:
                        reason = ApprovalReason.PROTECTED_REFERENCE_SLOTS_MISMATCH

            if reason is not None:
                self._binding_failures.append(ApprovalBindingFailure(reason))
                if self._mode is ApprovalMode.ENFORCE:
                    raise EffectApprovalRejected(reason)

            resolved_values = tuple(
                [
                    await self._resolver.resolve(reference, commit_descriptor.run_identity, consumer_id)
                    for reference in protected_references
                ]
            )
            consumed: ConsumedEffect | None = None
            try:
                consumed = _mint_consumed_effect()
                try:
                    return await dispatch(consumed, commit_descriptor, resolved_values)
                except BaseException as error:
                    # Completed trusted-sink frames can retain plaintext locals. Clear the complete
                    # exception graph without replacing types, messages, or traceback structure.
                    _clear_exception_frames(error)
                    raise
            finally:
                if consumed is not None:
                    object.__setattr__(consumed, "_active", False)
                    object.__setattr__(consumed, "_brand", None)
                del consumed
                del resolved_values
