import asyncio
import dataclasses
import inspect
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

import pytest

from skyvern.forge.sdk.browser_action_policy import ProtectedReference, ProtectedReferenceKind
from skyvern.forge.sdk.browser_effect_approval import (
    ApprovalBindingFailure,
    ApprovalId,
    ApprovalMode,
    ApprovalReason,
    ConsumedEffect,
    EffectApprovalRejected,
    EffectApprovalStore,
    EffectDescriptorFactory,
    FrozenEffectDescriptor,
    TrustedEffectDispatcher,
    canonicalize_effect_method,
    canonicalize_effect_target,
    canonicalize_non_secret_args,
)


@runtime_checkable
class _ConsumedBrowserActionApproval(Protocol):
    @property
    def consumption_id(self) -> str: ...


REFS = tuple(
    ProtectedReference(ProtectedReferenceKind.SECRET, reference_id, "wr_12880")
    for reference_id in ("ref_username", "ref_password")
)
SLOTS = ("field:username", "field:password")


class Resolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def resolve(self, ref: ProtectedReference, run_id: str, consumer_id: str) -> str:
        self.calls.append((ref.reference_id, run_id, consumer_id))
        return f"value:{ref.reference_id}"


class BrowserObject:
    pass


def effect(**changes: object) -> FrozenEffectDescriptor:
    base = FrozenEffectDescriptor(
        "run_1",
        "nonce_1",
        1,
        2,
        "page_1",
        "tab_1",
        "frame_1",
        "browser.click",
        "https://example.com/same",
        "click",
        (("button", '"submit"'),),
        tuple(ref.reference_id for ref in REFS),
    )
    return dataclasses.replace(base, **changes)


def sink(
    callback: Callable[[ConsumedEffect, FrozenEffectDescriptor, tuple[str, ...]], Awaitable[object]],
) -> TrustedEffectDispatcher[object]:
    return TrustedEffectDispatcher("playwright.click", REFS, callback, SLOTS)


def test_reason_codes_are_stable() -> None:
    assert {reason.value for reason in ApprovalReason} == {
        "fresh_approval_required",
        "approval_replayed",
        "run_identity_mismatch",
        "action_nonce_mismatch",
        "sink_sequence_mismatch",
        "observation_epoch_mismatch",
        "page_identity_mismatch",
        "tab_identity_mismatch",
        "frame_identity_mismatch",
        "sink_kind_mismatch",
        "canonical_target_mismatch",
        "canonical_method_mismatch",
        "non_secret_args_mismatch",
        "protected_reference_ids_mismatch",
        "protected_reference_slots_mismatch",
        "canonical_target_unsupported",
        "consumer_mismatch",
    }


def test_public_method_signatures_are_fixed() -> None:
    preview = inspect.signature(EffectApprovalStore.preview)
    assert tuple(preview.parameters) == ("self", "descriptor", "dispatcher")
    assert preview.return_annotation == "ApprovalId"

    consume = inspect.signature(EffectApprovalStore.consume_and_dispatch)
    assert tuple(consume.parameters) == ("self", "approval_id", "live_descriptor", "dispatcher")
    assert consume.return_annotation == "T"

    dispatch = inspect.signature(TrustedEffectDispatcher).parameters["dispatch"]
    assert dispatch.annotation == ("Callable[[ConsumedEffect, FrozenEffectDescriptor, tuple[str, ...]], Awaitable[T]]")


def test_only_in_dispatch_type_satisfies_causal_epoch_capability() -> None:
    approval_id = ApprovalId("approval_12880")

    assert not isinstance(approval_id, _ConsumedBrowserActionApproval)
    assert hasattr(ConsumedEffect, "consumption_id")

    with pytest.raises(TypeError, match="only be created"):
        ConsumedEffect()
    manually_allocated = object.__new__(ConsumedEffect)
    object.__setattr__(manually_allocated, "_active", True)
    object.__setattr__(manually_allocated, "_consumption_id", "fabricated")
    with pytest.raises(RuntimeError, match="not an active consumed effect"):
        _ = manually_allocated.consumption_id

    class StructuralFake:
        consumption_id = "fabricated"

    assert isinstance(StructuralFake(), _ConsumedBrowserActionApproval)
    assert not isinstance(StructuralFake(), ConsumedEffect)
    with pytest.raises(dataclasses.FrozenInstanceError):
        effect().run_identity = "run_2"  # type: ignore[misc]


def test_factory_owns_lineage_canonicalization_and_browser_identity() -> None:
    factory = EffectDescriptorFactory("run_1")
    first, second = factory.issue_lineage(), factory.issue_lineage()
    page, tab, frame = BrowserObject(), BrowserObject(), BrowserObject()

    def describe(
        *,
        target: str = "https://EXAMPLE.com:443/path?q=1#ignored",
        page_identity: object = page,
    ) -> FrozenEffectDescriptor:
        return factory.describe(
            first,
            observation_epoch=2,
            page=page_identity,
            tab=tab,
            frame=frame,
            sink_kind="browser.goto",
            target=target,
            method="get",
            non_secret_args={"z": [2, 1], "a": {"ok": True}},
            protected_references=REFS,
        )

    preview = describe()
    assert preview == describe(target="https://example.com/path?q=1")
    assert (first.sink_sequence, second.sink_sequence) == (0, 1)
    assert first.action_nonce != second.action_nonce  # allocated before dispatch; never a late action_id
    assert (preview.canonical_target, preview.canonical_method) == ("https://example.com/path?q=1", "GET")
    assert preview.non_secret_args == (("a", '{"ok":true}'), ("z", "[2,1]"))
    assert preview.protected_reference_ids == ("ref_username", "ref_password")
    changed = describe(page_identity=BrowserObject())
    assert changed.page_identity != preview.page_identity


def test_non_secret_args_refuse_protected_reference_objects() -> None:
    with pytest.raises(ValueError, match="only JSON"):
        canonicalize_non_secret_args({"credential": REFS[0]})
    with pytest.raises(ValueError, match="mapping"):
        canonicalize_non_secret_args([])  # type: ignore[arg-type]


def test_non_secret_args_reject_excessive_depth_and_shared_containers() -> None:
    nested: object = "leaf"
    for _ in range(65):
        nested = [nested]
    with pytest.raises(ValueError, match="only JSON"):
        canonicalize_non_secret_args({"nested": nested})

    shared: list[object] = ["leaf"]
    with pytest.raises(ValueError, match="only JSON"):
        canonicalize_non_secret_args({"dag": [shared, shared]})


def test_effect_target_uses_one_strict_browser_wire_form_without_scheme_aliases() -> None:
    assert canonicalize_effect_target("https://EXAMPLE.com:443/a%20path?q=%2F#ignored") == (
        "https://example.com/a%20path?q=%2F"
    )
    assert canonicalize_effect_target("ws://example.com/socket") == "ws://example.com/socket"
    assert canonicalize_effect_target("http://example.com/socket") == "http://example.com/socket"
    assert canonicalize_effect_target("ws://example.com/socket") != canonicalize_effect_target(
        "http://example.com/socket"
    )
    assert canonicalize_effect_target("https://127.0.0.1:444/path") == "https://127.0.0.1:444/path"
    assert canonicalize_effect_target("https://[2001:db8::1]/path") == "https://[2001:db8::1]/path"


@pytest.mark.parametrize("sub_delimiter", tuple("!$&()*+,;="))
def test_effect_target_query_allowlist_pins_each_safe_sub_delimiter(sub_delimiter: str) -> None:
    target = f"https://example.com/path?q={sub_delimiter}"
    assert canonicalize_effect_target(target) == target


def test_effect_target_keeps_browser_stable_query_brackets() -> None:
    target = "https://example.com/path?filter[status]=open"
    assert canonicalize_effect_target(target) == target


def test_effect_target_preserves_distinct_percent_escape_hex_case() -> None:
    lowercase = "https://example.com/a%2fpath?q=%2f%aa"
    uppercase = "https://example.com/a%2Fpath?q=%2F%AA"

    assert canonicalize_effect_target(lowercase) == lowercase
    assert canonicalize_effect_target(uppercase) == uppercase
    assert canonicalize_effect_target(lowercase) != canonicalize_effect_target(uppercase)
    assert canonicalize_effect_target(canonicalize_effect_target(lowercase)) == lowercase


@pytest.mark.parametrize(
    "target",
    (
        "https://example.com/café",
        "https://example.com/a/../admin",
        "https://example.com/a/%2e%2e/admin",
        "https://example.com/a space",
        "https://example.com/incomplete%2",
        "https://example.com/?quote='rewritten'",
        "https://example.com./trailing-root-dot",
        "https://example.com../multiple-root-dots",
        "https://a<b.example/",
        "https://a^b.example/",
        "https://a|b.example/",
        "https://a*b.example/",
        "https://[v1.fe80::]/",
        "https://[::ffff:127.0.0.1]/",
    ),
)
def test_effect_target_rejects_browser_rewrites_instead_of_matching_leniently(target: str) -> None:
    with pytest.raises(EffectApprovalRejected) as rejected:
        canonicalize_effect_target(target)
    assert rejected.value.reason is ApprovalReason.CANONICAL_TARGET_UNSUPPORTED


def test_method_and_json_canonicalization_reject_ambiguous_runtime_values() -> None:
    assert canonicalize_effect_method("get") == "GET"
    assert canonicalize_effect_method("delete") == "DELETE"
    assert canonicalize_effect_method("patch") == "patch"
    assert canonicalize_effect_method("PATCH") == "PATCH"
    assert canonicalize_effect_method("foo") != canonicalize_effect_method("FOO")
    with pytest.raises(ValueError, match="token"):
        canonicalize_effect_method(" get ")
    with pytest.raises(ValueError, match="token"):
        canonicalize_effect_method("ＧＥＴ")
    with pytest.raises(ValueError, match="only JSON"):
        canonicalize_non_secret_args({"coordinates": (1, 2)})
    with pytest.raises(ValueError, match="only JSON"):
        canonicalize_non_secret_args({"nested": {1: "value"}})


def test_descriptor_rejects_mutable_aliases_and_invalid_scalar_fields() -> None:
    mutable = ["approved"]
    with pytest.raises(TypeError, match=r"tuple\[str, str\]"):
        effect(non_secret_args=(("arg", mutable),))
    mutable[0] = "retargeted-after-preview"

    with pytest.raises(TypeError, match="non-empty strings"):
        effect(run_identity=[])
    with pytest.raises(ValueError, match="non-negative integers"):
        effect(sink_sequence=True)
    with pytest.raises(ValueError, match="non-negative integers"):
        effect(observation_epoch=1.0)


@pytest.mark.asyncio
async def test_preview_snapshots_descriptor_before_caller_mutation() -> None:
    async def dispatch(_: ConsumedEffect, __: FrozenEffectDescriptor, ___: tuple[str, ...]) -> object:
        raise AssertionError("a descriptor mutated after preview must not dispatch")

    dispatcher = sink(dispatch)
    approved = effect()
    store = EffectApprovalStore(mode=ApprovalMode.ENFORCE, resolver=Resolver())
    approval_id = store.preview(approved, dispatcher)
    object.__setattr__(approved, "non_secret_args", (("button", '"retargeted-after-preview"'),))

    with pytest.raises(EffectApprovalRejected) as rejected:
        await store.consume_and_dispatch(approval_id, approved, dispatcher)
    assert rejected.value.reason is ApprovalReason.NON_SECRET_ARGS_MISMATCH


@pytest.mark.parametrize(
    "slots",
    (
        ("field:username",),
        ("field:username", ""),
        ("field:username", "field:username"),
    ),
)
def test_dispatcher_rejects_desynchronized_protected_reference_slots(slots: tuple[str, ...]) -> None:
    async def dispatch(_: ConsumedEffect, __: FrozenEffectDescriptor, ___: tuple[str, ...]) -> object:
        return None

    with pytest.raises(TypeError, match="slots"):
        TrustedEffectDispatcher("playwright.click", REFS, dispatch, slots)


def test_public_authority_types_reject_incomplete_runtime_values() -> None:
    async def dispatch(_: ConsumedEffect, __: FrozenEffectDescriptor, ___: tuple[str, ...]) -> object:
        return None

    with pytest.raises(TypeError, match="Approval IDs"):
        ApprovalId("")
    incomplete = ProtectedReference(ProtectedReferenceKind.SECRET, "", "wr_12880")
    with pytest.raises(TypeError, match="immutable references"):
        TrustedEffectDispatcher("playwright.click", (incomplete,), dispatch, ("field:value",))


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("run_identity", "run_2", ApprovalReason.RUN_IDENTITY_MISMATCH),
        ("action_nonce", "nonce_2", ApprovalReason.ACTION_NONCE_MISMATCH),
        ("sink_sequence", 2, ApprovalReason.SINK_SEQUENCE_MISMATCH),
        ("observation_epoch", 3, ApprovalReason.OBSERVATION_EPOCH_MISMATCH),
        ("page_identity", "page_replaced", ApprovalReason.PAGE_IDENTITY_MISMATCH),
        ("tab_identity", "tab_2", ApprovalReason.TAB_IDENTITY_MISMATCH),
        ("frame_identity", "frame_2", ApprovalReason.FRAME_IDENTITY_MISMATCH),
        ("sink_kind", "browser.press", ApprovalReason.SINK_KIND_MISMATCH),
        ("canonical_target", "https://example.com/other", ApprovalReason.CANONICAL_TARGET_MISMATCH),
        ("canonical_method", "press", ApprovalReason.CANONICAL_METHOD_MISMATCH),
        ("non_secret_args", (("button", '"other"'),), ApprovalReason.NON_SECRET_ARGS_MISMATCH),
        ("protected_reference_ids", ("other",), ApprovalReason.PROTECTED_REFERENCE_IDS_MISMATCH),
    ),
)
@pytest.mark.asyncio
async def test_descriptor_mutation_burns_approval(field: str, value: object, reason: ApprovalReason) -> None:
    async def dispatch(_: ConsumedEffect, __: FrozenEffectDescriptor, ___: tuple[str, ...]) -> object:
        raise AssertionError("enforcement dispatched a mismatched effect")

    resolver, dispatcher = Resolver(), sink(dispatch)
    store, approved = EffectApprovalStore(mode=ApprovalMode.ENFORCE, resolver=resolver), effect()
    approval_id = store.preview(approved, dispatcher)
    with pytest.raises(EffectApprovalRejected) as rejected:
        await store.consume_and_dispatch(approval_id, dataclasses.replace(approved, **{field: value}), dispatcher)
    assert rejected.value.reason is reason
    assert resolver.calls == []
    with pytest.raises(EffectApprovalRejected) as replayed:
        await store.consume_and_dispatch(approval_id, approved, dispatcher)
    assert replayed.value.reason is ApprovalReason.APPROVAL_REPLAYED


@pytest.mark.asyncio
async def test_resolution_order_and_capability_lifetime_are_bound_to_dispatch() -> None:
    seen: list[ConsumedEffect] = []

    async def dispatch(
        consumed: ConsumedEffect, live_descriptor: FrozenEffectDescriptor, values: tuple[str, ...]
    ) -> object:
        seen.append(consumed)
        assert consumed.consumption_id
        assert live_descriptor == approved
        assert live_descriptor is not approved
        assert values == ("value:ref_username", "value:ref_password")
        assert dict(zip(SLOTS, values, strict=True)) == {
            "field:username": "value:ref_username",
            "field:password": "value:ref_password",
        }
        raise RuntimeError("sink failed")

    resolver, dispatcher = Resolver(), sink(dispatch)
    store, approved = EffectApprovalStore(mode=ApprovalMode.ENFORCE, resolver=resolver), effect()
    with pytest.raises(RuntimeError) as failed:
        await store.consume_and_dispatch(store.preview(approved, dispatcher), approved, dispatcher)
    assert str(failed.value) == "sink failed"
    assert "value:ref_" not in str(failed.value) + repr(store.__dict__)
    traceback = failed.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_name in {"consume_and_dispatch", "dispatch"}:
            assert "value:ref_" not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
    assert [call[0] for call in resolver.calls] == ["ref_username", "ref_password"]
    with pytest.raises(RuntimeError, match="no longer active"):
        _ = seen[0].consumption_id


@pytest.mark.asyncio
async def test_token_mint_failure_removes_resolved_values_from_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def dispatch(_: ConsumedEffect, __: FrozenEffectDescriptor, ___: tuple[str, ...]) -> object:
        raise AssertionError("token mint failure must happen before dispatch")

    dispatcher = sink(dispatch)
    approved = effect()
    store = EffectApprovalStore(mode=ApprovalMode.ENFORCE, resolver=Resolver())
    approval_id = store.preview(approved, dispatcher)

    def fail_to_mint(_: int) -> str:
        raise RuntimeError("entropy unavailable")

    monkeypatch.setattr("skyvern.forge.sdk.browser_effect_approval.secrets.token_urlsafe", fail_to_mint)
    with pytest.raises(RuntimeError, match="entropy unavailable") as failed:
        await store.consume_and_dispatch(approval_id, approved, dispatcher)

    traceback = failed.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_name == "consume_and_dispatch":
            assert "value:ref_" not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


@pytest.mark.asyncio
async def test_sink_failure_clears_protected_values_from_nested_exception_graph() -> None:
    async def dispatch(_: ConsumedEffect, __: FrozenEffectDescriptor, values: tuple[str, ...]) -> object:
        def fail_with_protected_local(protected_value: str) -> None:
            raise RuntimeError("inner sink failure")

        try:
            fail_with_protected_local(values[0])
        except RuntimeError as cause:
            wrapped = RuntimeError("outer sink failure")
            wrapped.__cause__ = cause
            raise ExceptionGroup("sink failures", [wrapped])

    dispatcher = sink(dispatch)
    approved = effect()
    store = EffectApprovalStore(mode=ApprovalMode.ENFORCE, resolver=Resolver())
    with pytest.raises(ExceptionGroup, match="sink failures") as failed:
        await store.consume_and_dispatch(store.preview(approved, dispatcher), approved, dispatcher)

    wrapped = failed.value.exceptions[0]
    cause = wrapped.__cause__
    assert cause is not None
    traceback = cause.__traceback__
    while traceback is not None:
        assert "value:ref_" not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


@pytest.mark.asyncio
async def test_protected_reference_slots_are_snapshot_and_revalidated() -> None:
    async def dispatch(_: ConsumedEffect, __: FrozenEffectDescriptor, ___: tuple[str, ...]) -> object:
        raise AssertionError("a dispatcher with changed slots must not run in enforcement")

    dispatcher = sink(dispatch)
    approved = effect()
    store = EffectApprovalStore(mode=ApprovalMode.ENFORCE, resolver=Resolver())
    approval_id = store.preview(approved, dispatcher)
    object.__setattr__(dispatcher, "protected_reference_slots", tuple(reversed(SLOTS)))

    with pytest.raises(EffectApprovalRejected) as rejected:
        await store.consume_and_dispatch(approval_id, approved, dispatcher)
    assert rejected.value.reason is ApprovalReason.PROTECTED_REFERENCE_SLOTS_MISMATCH


@pytest.mark.asyncio
async def test_protected_references_are_snapshot_and_revalidated() -> None:
    async def dispatch(_: ConsumedEffect, __: FrozenEffectDescriptor, ___: tuple[str, ...]) -> object:
        raise AssertionError("a dispatcher with changed references must not run in enforcement")

    dispatcher = sink(dispatch)
    approved = effect()
    store = EffectApprovalStore(mode=ApprovalMode.ENFORCE, resolver=Resolver())
    approval_id = store.preview(approved, dispatcher)
    object.__setattr__(dispatcher, "protected_references", tuple(reversed(REFS)))

    with pytest.raises(EffectApprovalRejected) as rejected:
        await store.consume_and_dispatch(approval_id, approved, dispatcher)
    assert rejected.value.reason is ApprovalReason.CONSUMER_MISMATCH


@pytest.mark.asyncio
async def test_protected_reference_values_are_snapshot_and_revalidated() -> None:
    reference = ProtectedReference(ProtectedReferenceKind.SECRET, "approved_ref", "wr_12880")

    async def dispatch(_: ConsumedEffect, __: FrozenEffectDescriptor, ___: tuple[str, ...]) -> object:
        raise AssertionError("a dispatcher with a changed reference must not run in enforcement")

    resolver = Resolver()
    dispatcher = TrustedEffectDispatcher("playwright.click", (reference,), dispatch, ("field:username",))
    approved = effect(protected_reference_ids=("approved_ref",))
    store = EffectApprovalStore(mode=ApprovalMode.ENFORCE, resolver=resolver)
    approval_id = store.preview(approved, dispatcher)
    object.__setattr__(reference, "reference_id", "swapped_ref")

    with pytest.raises(EffectApprovalRejected) as rejected:
        await store.consume_and_dispatch(approval_id, approved, dispatcher)
    assert rejected.value.reason is ApprovalReason.CONSUMER_MISMATCH
    assert resolver.calls == []


@pytest.mark.asyncio
async def test_dispatch_callback_and_consumer_id_are_snapshot_and_revalidated() -> None:
    calls: list[str] = []

    async def original(_: ConsumedEffect, __: FrozenEffectDescriptor, ___: tuple[str, ...]) -> object:
        calls.append("original")

    async def replacement(_: ConsumedEffect, __: FrozenEffectDescriptor, ___: tuple[str, ...]) -> object:
        calls.append("replacement")

    approved = effect()
    for field, replacement_value in (("dispatch", replacement), ("consumer_id", "replacement-consumer")):
        resolver = Resolver()
        dispatcher = sink(original)
        store = EffectApprovalStore(mode=ApprovalMode.ENFORCE, resolver=resolver)
        approval_id = store.preview(approved, dispatcher)
        object.__setattr__(dispatcher, field, replacement_value)

        with pytest.raises(EffectApprovalRejected) as rejected:
            await store.consume_and_dispatch(approval_id, approved, dispatcher)
        assert rejected.value.reason is ApprovalReason.CONSUMER_MISMATCH
        assert resolver.calls == []
    assert calls == []


@pytest.mark.asyncio
async def test_observe_mode_records_mutation_but_uses_live_dispatcher_snapshot() -> None:
    calls: list[str] = []

    async def original(_: ConsumedEffect, __: FrozenEffectDescriptor, ___: tuple[str, ...]) -> object:
        calls.append("original")

    async def replacement(_: ConsumedEffect, __: FrozenEffectDescriptor, ___: tuple[str, ...]) -> object:
        calls.append("replacement")

    resolver = Resolver()
    dispatcher = TrustedEffectDispatcher("original-consumer", REFS, original, SLOTS)
    approved = effect()
    store = EffectApprovalStore(mode=ApprovalMode.OBSERVE, resolver=resolver)
    approval_id = store.preview(approved, dispatcher)
    object.__setattr__(dispatcher, "consumer_id", "replacement-consumer")
    object.__setattr__(dispatcher, "dispatch", replacement)

    await store.consume_and_dispatch(approval_id, approved, dispatcher)

    assert calls == ["replacement"]
    assert {call[2] for call in resolver.calls} == {"replacement-consumer"}
    assert store.binding_failures == (ApprovalBindingFailure(ApprovalReason.CONSUMER_MISMATCH),)


@pytest.mark.asyncio
async def test_dispatcher_snapshot_cannot_change_during_protected_resolution() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class PausingResolver(Resolver):
        async def resolve(self, ref: ProtectedReference, run_id: str, consumer_id: str) -> str:
            self.calls.append((ref.reference_id, run_id, consumer_id))
            if len(self.calls) == 1:
                entered.set()
                await release.wait()
            return f"value:{ref.reference_id}"

    calls: list[str] = []

    async def original(_: ConsumedEffect, __: FrozenEffectDescriptor, ___: tuple[str, ...]) -> object:
        calls.append("original")

    async def replacement(_: ConsumedEffect, __: FrozenEffectDescriptor, ___: tuple[str, ...]) -> object:
        calls.append("replacement")

    resolver = PausingResolver()
    dispatcher = sink(original)
    approved = effect()
    store = EffectApprovalStore(mode=ApprovalMode.ENFORCE, resolver=resolver)
    task = asyncio.create_task(store.consume_and_dispatch(store.preview(approved, dispatcher), approved, dispatcher))
    await entered.wait()
    object.__setattr__(dispatcher, "consumer_id", "replacement-consumer")
    object.__setattr__(dispatcher, "dispatch", replacement)
    release.set()
    await task

    assert calls == ["original"]
    assert {call[2] for call in resolver.calls} == {"playwright.click"}


@pytest.mark.asyncio
async def test_commit_values_cannot_change_during_protected_resolution() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class PausingResolver(Resolver):
        async def resolve(self, ref: ProtectedReference, run_id: str, consumer_id: str) -> str:
            entered.set()
            await release.wait()
            self.calls.append((ref.reference_id, run_id, consumer_id))
            return f"value:{ref.reference_id}"

    seen: list[tuple[str, tuple[str, ...]]] = []

    async def dispatch(_: ConsumedEffect, live_descriptor: FrozenEffectDescriptor, values: tuple[str, ...]) -> object:
        seen.append((live_descriptor.canonical_target, values))

    reference = ProtectedReference(ProtectedReferenceKind.SECRET, "approved_ref", "wr_12880")
    dispatcher = TrustedEffectDispatcher("playwright.click", (reference,), dispatch, ("field:username",))
    approved = effect(protected_reference_ids=("approved_ref",))
    resolver = PausingResolver()
    store = EffectApprovalStore(mode=ApprovalMode.ENFORCE, resolver=resolver)
    task = asyncio.create_task(store.consume_and_dispatch(store.preview(approved, dispatcher), approved, dispatcher))
    await entered.wait()
    object.__setattr__(approved, "canonical_target", "https://retargeted.example/")
    object.__setattr__(reference, "reference_id", "swapped_ref")
    release.set()
    await task

    assert seen == [("https://example.com/same", ("value:approved_ref",))]
    assert resolver.calls == [("approved_ref", "run_1", "playwright.click")]


@pytest.mark.asyncio
async def test_dispatcher_identity_binding_and_observe_path() -> None:
    calls: list[str] = []

    async def dispatch(_: ConsumedEffect, __: FrozenEffectDescriptor, ___: tuple[str, ...]) -> object:
        calls.append("live")
        return "result"

    resolver, original, swapped = Resolver(), sink(dispatch), sink(dispatch)
    approved = effect()
    enforcing = EffectApprovalStore(mode=ApprovalMode.ENFORCE, resolver=resolver)
    approval_id = enforcing.preview(approved, original)
    with pytest.raises(EffectApprovalRejected) as rejected:
        await enforcing.consume_and_dispatch(approval_id, approved, swapped)
    assert rejected.value.reason is ApprovalReason.CONSUMER_MISMATCH
    assert calls == []  # identical claimed consumer and refs are insufficient; object identity binds

    observing = EffectApprovalStore(mode=ApprovalMode.OBSERVE, resolver=resolver)
    approval_id = observing.preview(approved, original)
    assert await observing.consume_and_dispatch(approval_id, approved, swapped) == "result"
    assert calls == ["live"]
    assert observing.binding_failures == (ApprovalBindingFailure(ApprovalReason.CONSUMER_MISMATCH),)


@pytest.mark.asyncio
async def test_sink_without_preview_replay_and_atomic_dispatch() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def dispatch(_: ConsumedEffect, __: FrozenEffectDescriptor, ___: tuple[str, ...]) -> object:
        entered.set()
        await release.wait()

    store, approved = EffectApprovalStore(mode=ApprovalMode.ENFORCE, resolver=Resolver()), effect()
    dispatcher = sink(dispatch)
    with pytest.raises(EffectApprovalRejected) as missing:
        unknown_approval = ApprovalId("not-previewed")
        await store.consume_and_dispatch(unknown_approval, approved, dispatcher)
    assert missing.value.reason is ApprovalReason.FRESH_APPROVAL_REQUIRED
    assert unknown_approval not in store._locks

    approval_id = store.preview(approved, dispatcher)
    first = asyncio.create_task(store.consume_and_dispatch(approval_id, approved, dispatcher))
    await entered.wait()
    replay = asyncio.create_task(store.consume_and_dispatch(approval_id, approved, dispatcher))
    await asyncio.sleep(0)
    assert not replay.done()  # mutation check: fails if compare/consume is separated from dispatch
    release.set()
    await first
    with pytest.raises(EffectApprovalRejected) as rejected:
        await replay
    assert rejected.value.reason is ApprovalReason.APPROVAL_REPLAYED
    assert store.preview(approved, dispatcher) != approval_id
