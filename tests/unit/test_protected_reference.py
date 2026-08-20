import asyncio
import dataclasses
import inspect
import traceback
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk import protected_reference as protected_reference_module
from skyvern.forge.sdk.browser_action_policy import ProtectedReference, ProtectedReferenceKind
from skyvern.forge.sdk.protected_reference import (
    ProtectedReferenceError,
    ProtectedReferenceErrorReason,
    ProtectedReferenceResolver,
    ProtectedReferenceStore,
    ProtectedValueResolver,
)

OWNER = "o_12876"
RUN = "wr_12876"
CONSUMER = "act_12876"
SECRET_REFERENCE_ID = "cred_12876_password"
SECRET_VALUE = "secret-value-that-must-not-leak"
RAW_FILE_PATH = "/private/run-12876/protected-document.pdf"


class RevealingResolver:
    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.calls = 0

    def __repr__(self) -> str:
        return f"RevealingResolver({SECRET_VALUE!r}, {RAW_FILE_PATH!r})"

    async def __call__(self) -> str:
        self.calls += 1
        if self.raises:
            raise RuntimeError(f"lookup failed for {SECRET_VALUE} at {RAW_FILE_PATH}")
        return SECRET_VALUE


def assert_protected_data_absent_from_module_traceback(error: BaseException) -> None:
    rendered = "".join(traceback.format_exception(error))
    assert SECRET_VALUE not in rendered
    assert RAW_FILE_PATH not in rendered
    traceback_node = error.__traceback__
    module_frames = 0
    while traceback_node is not None:
        if traceback_node.tb_frame.f_code.co_filename.endswith("/protected_reference.py"):
            module_frames += 1
            frame_locals = repr(traceback_node.tb_frame.f_locals)
            assert SECRET_VALUE not in frame_locals
            assert RAW_FILE_PATH not in frame_locals
        traceback_node = traceback_node.tb_next

    assert module_frames > 0


def bind_secret(store: ProtectedReferenceStore, loader: ProtectedValueResolver) -> ProtectedReference:
    return store.bind(
        kind=ProtectedReferenceKind.SECRET,
        owner_id=OWNER,
        run_id=RUN,
        consumer_id=CONSUMER,
        resolver=loader,
    )


def test_resolver_contract_is_consumer_bound() -> None:
    signature = inspect.signature(ProtectedReferenceResolver.resolve)

    assert inspect.iscoroutinefunction(ProtectedReferenceResolver.resolve)
    assert tuple(signature.parameters) == ("self", "ref", "run_id", "consumer_id")
    assert signature.parameters["ref"].annotation == "ProtectedReference"
    assert signature.parameters["run_id"].annotation == "str"
    assert signature.parameters["consumer_id"].annotation == "str"
    assert signature.return_annotation == "str"


def test_secret_binding_produces_an_opaque_reference_without_lookup() -> None:
    loader = AsyncMock(return_value=SECRET_VALUE)
    store = ProtectedReferenceStore()

    ref = bind_secret(store, loader)

    assert ref.kind is ProtectedReferenceKind.SECRET
    assert ref.owner_id == OWNER
    assert ref.reference_id.startswith("pref_")
    assert ref.complete
    loader.assert_not_awaited()
    assert SECRET_VALUE not in repr(ref)
    assert SECRET_VALUE not in repr(store)


@pytest.mark.asyncio
async def test_file_binding_uses_a_stable_opaque_capability_not_the_raw_path() -> None:
    loader = AsyncMock(return_value=RAW_FILE_PATH)
    store = ProtectedReferenceStore()

    ref = store.bind(
        kind=ProtectedReferenceKind.FILE,
        owner_id=OWNER,
        run_id=RUN,
        consumer_id=CONSUMER,
        resolver=loader,
    )

    assert ref.kind is ProtectedReferenceKind.FILE
    assert ref.owner_id == OWNER
    assert ref.reference_id.startswith("pref_")
    loader.assert_not_awaited()
    assert RAW_FILE_PATH not in repr(ref)
    assert RAW_FILE_PATH not in repr(store)
    reference_id = ref.reference_id
    assert await store.resolve(ref, RUN, CONSUMER) == RAW_FILE_PATH
    assert ref.reference_id == reference_id


def test_binding_api_cannot_treat_a_raw_llm_path_or_uri_as_authority() -> None:
    parameters = inspect.signature(ProtectedReferenceStore.bind).parameters

    assert "reference_id" not in parameters
    assert "path" not in parameters
    assert "uri" not in parameters


def test_each_binding_gets_a_distinct_opaque_capability() -> None:
    loader = AsyncMock(return_value=SECRET_VALUE)
    store = ProtectedReferenceStore()

    first = bind_secret(store, loader)
    second = bind_secret(store, loader)

    assert first.reference_id != second.reference_id


def test_reference_id_collision_retries_without_replacing_a_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    generated = iter(["collision", "collision", "unique"])
    monkeypatch.setattr(protected_reference_module.secrets, "token_urlsafe", lambda _: next(generated))
    loader = AsyncMock(return_value=SECRET_VALUE)
    store = ProtectedReferenceStore()

    first = bind_secret(store, loader)
    second = bind_secret(store, loader)

    assert first.reference_id == "pref_collision"
    assert second.reference_id == "pref_unique"


@pytest.mark.parametrize(("field", "value"), [("kind", "secret"), ("resolver", object())])
def test_binding_rejects_untyped_kinds_and_noncallable_resolvers(field: str, value: object) -> None:
    arguments = {
        "kind": ProtectedReferenceKind.SECRET,
        "owner_id": OWNER,
        "run_id": RUN,
        "consumer_id": CONSUMER,
        "resolver": AsyncMock(return_value=SECRET_VALUE),
        field: value,
    }

    with pytest.raises(ProtectedReferenceError) as caught:
        ProtectedReferenceStore().bind(**arguments)

    assert caught.value.reason is ProtectedReferenceErrorReason.INCOMPLETE_BINDING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replacement", "resolve_run", "resolve_consumer"),
    [
        ({"reference_id": "cred_other"}, RUN, CONSUMER),
        ({"owner_id": "o_other"}, RUN, CONSUMER),
        ({"kind": ProtectedReferenceKind.FILE}, RUN, CONSUMER),
        ({}, "wr_other", CONSUMER),
        ({}, RUN, "act_other"),
        ({"reference_id": RAW_FILE_PATH, "owner_id": SECRET_VALUE}, RUN, CONSUMER),
    ],
)
async def test_every_capability_field_is_authorized_before_resolution(
    replacement: dict[str, object], resolve_run: str, resolve_consumer: str
) -> None:
    loader = RevealingResolver()
    store = ProtectedReferenceStore()
    ref = dataclasses.replace(bind_secret(store, loader), **replacement)

    with pytest.raises(ProtectedReferenceError) as caught:
        await store.resolve(ref, resolve_run, resolve_consumer)

    assert caught.value.reason is ProtectedReferenceErrorReason.NOT_AUTHORIZED
    assert ref.reference_id not in str(caught.value)
    assert OWNER not in str(caught.value)
    assert RUN not in str(caught.value)
    assert CONSUMER not in str(caught.value)
    assert loader.calls == 0
    assert_protected_data_absent_from_module_traceback(caught.value)


@pytest.mark.asyncio
async def test_resolution_occurs_only_after_the_exact_binding_is_authorized() -> None:
    loader = AsyncMock(return_value=SECRET_VALUE)
    store = ProtectedReferenceStore()
    ref = bind_secret(store, loader)

    resolved = await store.resolve(ref, RUN, CONSUMER)

    assert resolved == SECRET_VALUE
    loader.assert_awaited_once_with()
    assert SECRET_VALUE not in repr(store)


@pytest.mark.parametrize(
    "fields",
    [
        {"owner_id": ""},
        {"owner_id": "   "},
        {"run_id": ""},
        {"consumer_id": ""},
    ],
)
def test_binding_fails_closed_on_incomplete_ownership_facts(fields: dict[str, str]) -> None:
    loader = RevealingResolver()
    arguments = {
        "kind": ProtectedReferenceKind.SECRET,
        "owner_id": OWNER,
        "run_id": RUN,
        "consumer_id": CONSUMER,
        "resolver": loader,
        **fields,
    }

    with pytest.raises(ProtectedReferenceError) as caught:
        ProtectedReferenceStore().bind(**arguments)

    assert caught.value.reason is ProtectedReferenceErrorReason.INCOMPLETE_BINDING
    assert SECRET_VALUE not in str(caught.value)
    assert loader.calls == 0
    assert_protected_data_absent_from_module_traceback(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reference", "run_id", "consumer_id"),
    [
        (ProtectedReference(ProtectedReferenceKind.SECRET, RAW_FILE_PATH, ""), SECRET_VALUE, CONSUMER),
        (ProtectedReference(ProtectedReferenceKind.SECRET, "", OWNER), RUN, CONSUMER),
        (ProtectedReference(ProtectedReferenceKind.SECRET, SECRET_REFERENCE_ID, OWNER), "", CONSUMER),
        (ProtectedReference(ProtectedReferenceKind.SECRET, SECRET_REFERENCE_ID, OWNER), RUN, ""),
    ],
)
async def test_resolver_fails_closed_before_lookup_on_incomplete_ownership_facts(
    reference: ProtectedReference, run_id: str, consumer_id: str
) -> None:
    loader = RevealingResolver()
    store = ProtectedReferenceStore()
    bind_secret(store, loader)

    with pytest.raises(ProtectedReferenceError) as caught:
        await store.resolve(reference, run_id, consumer_id)

    assert caught.value.reason is ProtectedReferenceErrorReason.INCOMPLETE_BINDING
    assert loader.calls == 0
    assert_protected_data_absent_from_module_traceback(caught.value)


@pytest.mark.asyncio
async def test_loader_failures_are_wrapped_without_sensitive_exception_context() -> None:
    loader = RevealingResolver(raises=True)
    store = ProtectedReferenceStore()
    ref = bind_secret(store, loader)

    with pytest.raises(ProtectedReferenceError) as caught:
        await store.resolve(ref, RUN, CONSUMER)

    assert caught.value.reason is ProtectedReferenceErrorReason.RESOLUTION_FAILED
    assert caught.value.__context__ is None
    assert_protected_data_absent_from_module_traceback(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved", [None, "", 7])
async def test_loader_must_return_a_nonempty_string(resolved: object) -> None:
    loader = AsyncMock(return_value=resolved)
    store = ProtectedReferenceStore()
    ref = bind_secret(store, loader)

    with pytest.raises(ProtectedReferenceError) as caught:
        await store.resolve(ref, RUN, CONSUMER)

    assert caught.value.reason is ProtectedReferenceErrorReason.RESOLUTION_FAILED


@pytest.mark.asyncio
async def test_invalid_loader_value_is_removed_from_exception_traceback_locals() -> None:
    loader = AsyncMock(return_value=f"{SECRET_VALUE} {RAW_FILE_PATH}".encode())
    store = ProtectedReferenceStore()
    ref = bind_secret(store, loader)

    with pytest.raises(ProtectedReferenceError) as caught:
        await store.resolve(ref, RUN, CONSUMER)

    assert_protected_data_absent_from_module_traceback(caught.value)


@pytest.mark.asyncio
async def test_cancellation_preserves_semantics_without_protected_exception_state() -> None:
    started = asyncio.Event()

    async def loader() -> str:
        protected_value = f"{SECRET_VALUE} {RAW_FILE_PATH}"
        started.set()
        await asyncio.sleep(60)
        return protected_value

    store = ProtectedReferenceStore()
    ref = bind_secret(store, loader)
    task = asyncio.create_task(store.resolve(ref, RUN, CONSUMER))
    await started.wait()
    task.cancel(f"{SECRET_VALUE} {RAW_FILE_PATH}")

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert task.cancelled()
    assert caught.value.__context__ is None
    assert_protected_data_absent_from_module_traceback(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [BaseException, KeyboardInterrupt, SystemExit, GeneratorExit])
async def test_process_control_errors_preserve_type_without_protected_exception_state(
    error_type: type[BaseException],
) -> None:
    message: object = 7 if error_type is SystemExit else f"{SECRET_VALUE} {RAW_FILE_PATH}"

    async def loader() -> str:
        raise error_type(message)

    store = ProtectedReferenceStore()
    ref = bind_secret(store, loader)

    with pytest.raises(error_type) as caught:
        await store.resolve(ref, RUN, CONSUMER)

    assert caught.value.__context__ is None
    if isinstance(caught.value, SystemExit):
        assert caught.value.code == 7
    assert_protected_data_absent_from_module_traceback(caught.value)


@pytest.mark.asyncio
async def test_base_exception_groups_are_recursively_sanitized() -> None:
    group = BaseExceptionGroup(
        f"group {SECRET_VALUE} {RAW_FILE_PATH}",
        [SystemExit(SECRET_VALUE), BaseExceptionGroup(RAW_FILE_PATH, [KeyboardInterrupt(SECRET_VALUE)])],
    )

    async def loader() -> str:
        raise group

    store = ProtectedReferenceStore()
    with pytest.raises(BaseExceptionGroup) as caught:
        await store.resolve(bind_secret(store, loader), RUN, CONSUMER)
    assert caught.value.__context__ is None
    assert isinstance(caught.value.exceptions[0], SystemExit)
    assert isinstance(caught.value.exceptions[1], BaseExceptionGroup)
    assert_protected_data_absent_from_module_traceback(caught.value)
