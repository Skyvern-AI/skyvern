"""Per-run browser-engine selection (OSS seam).

These tests stay driver-agnostic: they exercise the registry, per-run selection, capability gate,
and exception-identity classification with fake engine specs, so they hold on an image that ships
only stock Playwright. The rustwright cases simulate the driver package's presence or absence
explicitly, so they assert behaviour and capability, never whether the optional group is installed.
Cloud-only concerns (the cloud-private engine, the multivariate flag) are left to the cloud wiring
slice that introduces them.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest
from playwright._impl._errors import TargetClosedError as PlaywrightTargetClosedError
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.webeye import browser_engine
from skyvern.webeye.browser_engine import (
    RUSTWRIGHT_ALLOWED_BROWSER_SOURCES,
    STOCK_ENGINE_NAME,
    BrowserEngineContext,
    BrowserEngineMetadata,
    BrowserEngineRegistry,
    BrowserEngineRichErrorTypes,
    BrowserEngineSelection,
    BrowserEngineSpec,
    BrowserEngineUnavailable,
    BrowserSourceNotSupportedByEngine,
    UnknownBrowserEngine,
    resolve_browser_engine,
)
from skyvern.webeye.browser_errors import (
    BrowserAutomationError,
    BrowserCdpConnectionError,
    BrowserEngineErrorFamilies,
    BrowserErrorFamiliesConfigError,
    BrowserRetryableCdpError,
    BrowserTargetClosedError,
    BrowserTimeoutError,
)


class _EngineAError(Exception):
    pass


class _EngineATimeout(_EngineAError):
    pass


class _EngineBError(Exception):
    pass


class _EngineBTimeout(_EngineBError):
    pass


async def _never_start():  # pragma: no cover - must never be awaited in gate tests
    raise AssertionError("start_driver must not be called when a capability gate rejects the source")


def _selection(
    name: str,
    error_type: type[BaseException],
    timeout_type: type[BaseException],
    *,
    allowed_sources: frozenset[str] | None = None,
    start=_never_start,
    target_closed_types: tuple[type[BaseException], ...] = (),
    cdp_connection_types: tuple[type[BaseException], ...] = (),
    retryable_types: tuple[type[BaseException], ...] = (),
) -> BrowserEngineSelection:
    return BrowserEngineSelection(
        name=name,
        start_driver=start,
        error_type=error_type,
        timeout_error_type=timeout_type,
        target_closed_error_types=target_closed_types,
        cdp_connection_error_types=cdp_connection_types,
        retryable_error_types=retryable_types,
        metadata=BrowserEngineMetadata(name=name, version="0.0.0", allowed_browser_sources=allowed_sources),
        selection_reason="test",
    )


@pytest.fixture(autouse=True)
def _restore_resolver():
    yield
    browser_engine.reset_browser_engine_resolver()


_RUSTWRIGHT_DRIVER_MODULES = ("rustwright", "rustwright.async_api")


class _RustwrightImportBlocker:
    """Meta-path finder that fails every ``rustwright`` import with the same ModuleNotFoundError an
    image without the wheel raises, so the fail-closed-when-absent contract is verified deterministically
    instead of relying on the ambient environment lacking the package."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "rustwright" or fullname.startswith("rustwright."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name="rustwright")
        return None


@contextlib.contextmanager
def _rustwright_driver_absent():
    saved = {name: sys.modules.get(name) for name in _RUSTWRIGHT_DRIVER_MODULES}
    for name in _RUSTWRIGHT_DRIVER_MODULES:
        sys.modules.pop(name, None)
    blocker = _RustwrightImportBlocker()
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)
        for name, module in saved.items():
            if module is not None:
                sys.modules[name] = module


@contextlib.contextmanager
def _rustwright_driver_present():
    """Make ``rustwright.async_api`` importable with the exact public surface the OSS module binds
    (``Error`` / ``TimeoutError`` / ``TargetClosedError`` in the hierarchy the real wheel documents), so
    the group-installed behaviour is exercised without depending on a real install of the PyO3 wheel."""
    saved = {name: sys.modules.get(name) for name in _RUSTWRIGHT_DRIVER_MODULES}
    package = types.ModuleType("rustwright")
    async_api = types.ModuleType("rustwright.async_api")

    class Error(Exception):
        pass

    class TimeoutError(Error):
        pass

    class TargetClosedError(Error):
        pass

    async def async_playwright():  # pragma: no cover - a selection never starts the driver here
        raise AssertionError("the rustwright driver must not be started in these tests")

    async_api.Error = Error
    async_api.TimeoutError = TimeoutError
    async_api.TargetClosedError = TargetClosedError
    async_api.async_playwright = async_playwright
    package.async_api = async_api
    sys.modules["rustwright"] = package
    sys.modules["rustwright.async_api"] = async_api
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def test_registry_has_stock_and_rustwright_and_rejects_unknown():
    names = browser_engine.REGISTRY.names()
    assert STOCK_ENGINE_NAME in names
    assert browser_engine.RUSTWRIGHT_ENGINE_NAME in names
    with pytest.raises(UnknownBrowserEngine):
        browser_engine.REGISTRY.get("no-such-engine")


def test_registry_rejects_duplicate_registration():
    registry = BrowserEngineRegistry()
    spec = BrowserEngineSpec(
        name="dup", _start_driver=_never_start, _load_error_types=lambda: (_EngineAError, _EngineATimeout)
    )
    registry.register(spec)
    with pytest.raises(ValueError):
        registry.register(spec)


def test_selection_is_frozen():
    sel = _selection("playwright", PlaywrightError, PlaywrightTimeoutError)
    with pytest.raises(dataclasses.FrozenInstanceError):
        sel.name = "mutated"  # type: ignore[misc]


def test_stock_spec_selects_with_playwright_identity():
    sel = browser_engine.REGISTRY.get(STOCK_ENGINE_NAME).select(selection_reason="test")
    assert sel.name == STOCK_ENGINE_NAME
    assert sel.metadata.allowed_browser_sources is None
    assert sel.is_engine_error(PlaywrightError("boom"))
    assert sel.is_engine_timeout_error(PlaywrightTimeoutError("slow"))
    assert not sel.is_engine_error(ValueError("unrelated"))


def test_rustwright_spec_fails_closed_when_driver_absent():
    # With the driver package absent, selecting rustwright must fail closed, never fall back. Absence is
    # forced here rather than read from the environment, so the contract holds even where the optional
    # group is installed.
    spec = browser_engine.REGISTRY.get(browser_engine.RUSTWRIGHT_ENGINE_NAME)
    with _rustwright_driver_absent():
        assert spec.is_installed() is False
        with pytest.raises(browser_engine.BrowserEngineUnavailable):
            spec.select(selection_reason="explicit-rustwright")


def test_rustwright_spec_has_rich_error_loader_wired_and_stays_lazy():
    # The rustwright spec carries the rich loader by reference, and registering it never imports the
    # driver. Laziness is probed in a fresh subprocess so it holds whether or not the wheel is installed,
    # instead of asserting about this process's already-populated sys.modules.
    spec = browser_engine.REGISTRY.get(browser_engine.RUSTWRIGHT_ENGINE_NAME)
    assert spec._load_rich_error_types is browser_engine._rustwright_rich_error_types
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, skyvern.webeye.browser_engine; "
            "print(any(m == 'rustwright' or m.startswith('rustwright.') for m in sys.modules))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.strip() == "False", probe.stdout + probe.stderr


def test_rustwright_rich_loader_imports_the_real_driver_and_fails_closed_when_absent():
    # The rich loader binds the REAL native identity (a lazy ``from rustwright.async_api import
    # TargetClosedError``), not a stub: with the package absent it raises ModuleNotFoundError naming
    # the driver, and select() surfaces that as BrowserEngineUnavailable (never silent empty families).
    with _rustwright_driver_absent():
        with pytest.raises(ModuleNotFoundError) as excinfo:
            browser_engine._rustwright_rich_error_types()
        assert excinfo.value.name == "rustwright"
        with pytest.raises(browser_engine.BrowserEngineUnavailable):
            browser_engine.RUSTWRIGHT_SPEC.select(selection_reason="explicit-rustwright")


def test_installing_rustwright_group_keeps_it_deny_all_not_selectable():
    # The blocker this guards: merely installing the optional rustwright group must not make the engine
    # selectable. With the driver importable, select() succeeds (no BrowserEngineUnavailable), but the
    # selection stays deny-all — every source, attributed or not, is rejected before any provisioning —
    # so installing the wheel never turns rustwright into a routable engine.
    spec = browser_engine.REGISTRY.get(browser_engine.RUSTWRIGHT_ENGINE_NAME)
    with _rustwright_driver_present():
        assert spec.is_installed() is True
        selection = spec.select(selection_reason="explicit-rustwright")
        assert selection.metadata.allowed_browser_sources == frozenset()
        for source in ("chromium-headful", "cdp-connect", None):
            with pytest.raises(BrowserSourceNotSupportedByEngine):
                selection.ensure_supports(source)


# Representative neutral source strings (not a canonical source registry — that belongs to the cloud
# wiring slice). Deny-all is source-independent: it must reject whatever source it is handed, so the
# sample only needs to be varied, including the ``None`` unattributed case.
SAMPLE_BROWSER_SOURCES = ("chromium-headful", "chromium-headless", "cdp-connect", None)


@pytest.mark.asyncio
async def test_rustwright_deny_all_fails_before_provisioning_regardless_of_source():
    # Empty capability set = deny-all: the adapter contract exists but no source may select it. Each
    # source must raise BEFORE start_driver (_never_start) runs, so explicit Rustwright cannot start a
    # driver in production today — it stays rollout-incapable until exception normalization lands.
    assert RUSTWRIGHT_ALLOWED_BROWSER_SOURCES == frozenset()
    denied = _selection(
        "rustwright", _EngineAError, _EngineATimeout, allowed_sources=RUSTWRIGHT_ALLOWED_BROWSER_SOURCES
    )
    browser_engine.set_browser_engine_resolver(lambda ctx: _async(denied))
    for source in SAMPLE_BROWSER_SOURCES:
        with pytest.raises(BrowserSourceNotSupportedByEngine):
            await resolve_browser_engine(BrowserEngineContext(browser_source=source))


@pytest.mark.asyncio
async def test_unrestricted_engine_allows_unattributed_source():
    # allowed_browser_sources is None => unrestricted: a run with no attributed source is served.
    unrestricted = _selection("engine-a", _EngineAError, _EngineATimeout, allowed_sources=None, start=_ok_start)
    browser_engine.set_browser_engine_resolver(lambda ctx: _async(unrestricted))
    sel = await resolve_browser_engine(BrowserEngineContext(browser_source=None))
    assert sel.name == "engine-a"


@pytest.mark.asyncio
async def test_restricted_engine_rejects_unattributed_source():
    # A restricted engine must fail closed on an unattributed source, and attribute it honestly (None),
    # not as the misleading string "None".
    restricted = _selection("engine-a", _EngineAError, _EngineATimeout, allowed_sources=frozenset({"cdp-connect"}))
    browser_engine.set_browser_engine_resolver(lambda ctx: _async(restricted))
    with pytest.raises(BrowserSourceNotSupportedByEngine) as excinfo:
        await resolve_browser_engine(BrowserEngineContext(browser_source=None))
    assert excinfo.value.browser_source is None


@pytest.mark.asyncio
async def test_deny_all_rejects_unattributed_source():
    denied = _selection(
        "rustwright", _EngineAError, _EngineATimeout, allowed_sources=RUSTWRIGHT_ALLOWED_BROWSER_SOURCES
    )
    browser_engine.set_browser_engine_resolver(lambda ctx: _async(denied))
    with pytest.raises(BrowserSourceNotSupportedByEngine):
        await resolve_browser_engine(BrowserEngineContext(browser_source=None))


@pytest.mark.asyncio
async def test_restricted_engine_allows_known_source():
    restricted = _selection(
        "engine-a", _EngineAError, _EngineATimeout, allowed_sources=frozenset({"cdp-connect"}), start=_ok_start
    )
    browser_engine.set_browser_engine_resolver(lambda ctx: _async(restricted))
    sel = await resolve_browser_engine(BrowserEngineContext(browser_source="cdp-connect"))
    assert sel.name == "engine-a"


@pytest.mark.asyncio
async def test_restricted_engine_rejects_disallowed_known_source():
    restricted = _selection("engine-a", _EngineAError, _EngineATimeout, allowed_sources=frozenset({"cdp-connect"}))
    browser_engine.set_browser_engine_resolver(lambda ctx: _async(restricted))
    with pytest.raises(BrowserSourceNotSupportedByEngine) as excinfo:
        await resolve_browser_engine(BrowserEngineContext(browser_source="chromium-headful"))
    assert excinfo.value.browser_source == "chromium-headful"


@pytest.mark.asyncio
async def test_rejected_unattributed_source_does_not_start_driver():
    started = False

    async def _spy_start():
        nonlocal started
        started = True
        return object()

    denied = _selection(
        "rustwright",
        _EngineAError,
        _EngineATimeout,
        allowed_sources=RUSTWRIGHT_ALLOWED_BROWSER_SOURCES,
        start=_spy_start,
    )
    browser_engine.set_browser_engine_resolver(lambda ctx: _async(denied))
    with pytest.raises(BrowserSourceNotSupportedByEngine):
        await resolve_browser_engine(BrowserEngineContext(browser_source=None))
    assert started is False


@pytest.mark.asyncio
async def test_default_resolver_is_stock_playwright():
    sel = await resolve_browser_engine(BrowserEngineContext(browser_source="local-browser"))
    assert sel.name == STOCK_ENGINE_NAME


@pytest.mark.asyncio
async def test_default_resolver_stays_stock_even_when_rustwright_installed():
    # Installing the optional group must not shift the OSS default runtime selection.
    with _rustwright_driver_present():
        sel = await resolve_browser_engine(BrowserEngineContext(browser_source="local-browser"))
    assert sel.name == STOCK_ENGINE_NAME


@pytest.mark.asyncio
async def test_unknown_engine_selection_fails_closed():
    browser_engine.set_browser_engine_resolver(
        lambda ctx: browser_engine.REGISTRY.get("phantom").select(selection_reason="x")  # type: ignore[return-value]
    )
    with pytest.raises(UnknownBrowserEngine):
        await resolve_browser_engine(BrowserEngineContext(browser_source="local-browser"))


@pytest.mark.asyncio
async def test_concurrent_runs_pin_distinct_engines_without_global_rebinding():
    a = _selection("engine-a", _EngineAError, _EngineATimeout, start=_ok_start)
    b = _selection("engine-b", _EngineBError, _EngineBTimeout, start=_ok_start)

    async def resolver(ctx: BrowserEngineContext) -> BrowserEngineSelection:
        # Simulate interleaving: yield so the two runs overlap inside the resolver.
        await asyncio.sleep(0)
        return a if ctx.workflow_run_id == "run-a" else b

    browser_engine.set_browser_engine_resolver(resolver)
    sel_a, sel_b = await asyncio.gather(
        resolve_browser_engine(BrowserEngineContext(workflow_run_id="run-a", browser_source="local-browser")),
        resolve_browser_engine(BrowserEngineContext(workflow_run_id="run-b", browser_source="local-browser")),
    )
    assert sel_a.name == "engine-a" and sel_b.name == "engine-b"
    # Each run's exception identity is its own and cannot be invalidated by the other run.
    assert sel_a.is_engine_error(_EngineAError()) and not sel_a.is_engine_error(_EngineBError())
    assert sel_b.is_engine_error(_EngineBError()) and not sel_b.is_engine_error(_EngineAError())


def test_selection_pinning_survives_resolver_change_midrun():
    # Once resolved, a selection object holds its own engine; later resolver swaps cannot mutate it.
    pinned = _selection("engine-a", _EngineAError, _EngineATimeout)
    browser_engine.set_browser_engine_resolver(
        lambda ctx: _async(_selection("engine-b", _EngineBError, _EngineBTimeout))
    )
    assert pinned.name == "engine-a"
    assert pinned.is_engine_error(_EngineAError())
    assert not pinned.is_engine_error(_EngineBError())


def test_oss_module_only_references_oss_safe_driver_packages():
    # The OSS seam must not name any cloud-private driver package. Assert positively — every
    # driver import in the module targets an OSS-safe package — so this test file never has to
    # embed a cloud-private identifier itself (tests/unit/ is synced to the public repo).
    source = Path(browser_engine.__file__).read_text()
    driver_packages = set(re.findall(r"from (\w+)\.async_api", source))
    assert driver_packages <= {"playwright", "rustwright"}, driver_packages
    assert browser_engine.REGISTRY.names() >= {STOCK_ENGINE_NAME, browser_engine.RUSTWRIGHT_ENGINE_NAME}


def test_selection_binds_base_timeout_error_families_from_engine_identities():
    # The selection owns an immutable BrowserEngineErrorFamilies built from the exact package
    # identities the spec loaded lazily at select() time (base + timeout only — the stable public
    # identities every driver exposes).
    sel = browser_engine.REGISTRY.get(STOCK_ENGINE_NAME).select(selection_reason="test")
    assert isinstance(sel.error_families, BrowserEngineErrorFamilies)
    assert sel.error_families.base_error_types == (PlaywrightError,)
    assert sel.error_families.timeout_types == (PlaywrightTimeoutError,)


def test_selection_classify_error_maps_native_timeout_and_base_preserving_cause():
    sel = browser_engine.REGISTRY.get(STOCK_ENGINE_NAME).select(selection_reason="test")

    native_timeout = PlaywrightTimeoutError("navigation timed out")
    classified_timeout = sel.classify_error(native_timeout)
    assert type(classified_timeout) is BrowserTimeoutError
    assert classified_timeout.__cause__ is native_timeout

    native_base = PlaywrightError("generic driver failure")
    classified_base = sel.classify_error(native_base)
    assert type(classified_base) is BrowserAutomationError
    assert classified_base.__cause__ is native_base


def test_selection_classify_error_returns_none_for_foreign_and_unknown():
    # A foreign engine's native error and an unrelated stdlib error are both invisible to this
    # engine's families, so the caller must re-raise them (None, never swallowed).
    sel = browser_engine.REGISTRY.get(STOCK_ENGINE_NAME).select(selection_reason="test")
    assert sel.classify_error(_EngineAError("foreign engine error")) is None
    assert sel.classify_error(ValueError("unrelated")) is None


def test_selection_classify_error_is_bound_to_its_own_engine_identity():
    # Adapter-bound: a selection classifies only its own driver family, not another engine's, even
    # when the two engines' class names/hierarchies mirror each other.
    sel_a = _selection("engine-a", _EngineAError, _EngineATimeout)
    assert type(sel_a.classify_error(_EngineATimeout("slow"))) is BrowserTimeoutError
    assert type(sel_a.classify_error(_EngineAError("boom"))) is BrowserAutomationError
    assert sel_a.classify_error(_EngineBError("other engine")) is None
    assert sel_a.classify_error(_EngineBTimeout("other engine slow")) is None


def test_directly_constructed_selection_derives_error_families_and_stays_helper_compatible():
    # A selection built without an explicit families object (as the existing constructors do) still
    # derives its families, and the derivation does not disturb the is_engine_* helpers.
    sel = _selection("engine-a", _EngineAError, _EngineATimeout)
    assert sel.error_families.base_error_types == (_EngineAError,)
    assert sel.error_families.timeout_types == (_EngineATimeout,)
    assert sel.is_engine_error(_EngineAError())
    assert sel.is_engine_timeout_error(_EngineATimeout())
    assert not sel.is_engine_error(_EngineBError())


def test_same_class_base_and_timeout_derives_single_family_and_classifies_as_timeout():
    # Compatibility branch: an engine (or fake) that reports the SAME class for both its base and
    # timeout identity must not trip the one-type-per-family guard. The shared class lands only in the
    # timeout family (the more-specific view classification checks first), base_error_types is empty,
    # and the class still classifies — as a timeout, with __cause__ preserved. The is_engine_* helpers
    # stay true because they check the raw identities, independent of the derived families.
    class SharedError(Exception):
        pass

    sel = _selection("engine-shared", SharedError, SharedError)
    assert sel.error_families.base_error_types == ()
    assert sel.error_families.timeout_types == (SharedError,)

    original = SharedError("shared native error")
    classified = sel.classify_error(original)
    assert type(classified) is BrowserTimeoutError
    assert classified.__cause__ is original

    assert sel.is_engine_error(SharedError())
    assert sel.is_engine_timeout_error(SharedError())


def test_reverse_hierarchy_timeout_over_base_rejects_construction():
    # If the base error subclasses the timeout (reverse of every real driver), a plain base error
    # would classify as a timeout and is_engine_error/is_engine_timeout_error would disagree. The
    # selection must fail loudly at construction rather than bind a misleading engine.
    class _Parent(Exception):
        pass

    class _Child(_Parent):
        pass

    with pytest.raises(BrowserErrorFamiliesConfigError):
        _selection("engine-reverse", error_type=_Child, timeout_type=_Parent)


def test_unrelated_base_and_timeout_identities_reject_construction():
    # Unrelated identities would leave a timeout outside the engine's error family; reject them.
    class _Base(Exception):
        pass

    class _Unrelated(Exception):
        pass

    with pytest.raises(BrowserErrorFamiliesConfigError):
        _selection("engine-unrelated", error_type=_Base, timeout_type=_Unrelated)


# --- S34: richer per-selection error-family binding ---------------------------------------------
#
# A fake engine whose native classes span every richer family. Each subclasses the engine base error
# (an engine's own target-closed/CDP/retryable errors are engine errors), and retryable subclasses
# CDP-connection so a single instance matches both families via inheritance — this is what proves the
# classifier's retryable-before-CDP precedence survives the selection binding.
class _RichEngineError(Exception):
    pass


class _RichEngineTimeout(_RichEngineError):
    pass


class _RichTargetClosed(_RichEngineError):
    pass


class _RichCdpConnection(_RichEngineError):
    pass


class _RichRetryable(_RichCdpConnection):
    pass


def _rich_selection() -> BrowserEngineSelection:
    return _selection(
        "engine-rich",
        _RichEngineError,
        _RichEngineTimeout,
        target_closed_types=(_RichTargetClosed,),
        cdp_connection_types=(_RichCdpConnection,),
        retryable_types=(_RichRetryable,),
    )


def test_stock_selection_binds_target_closed_family_from_native_identity():
    # Stock Playwright exposes a distinct native target-closed class; the selection must bind it as a
    # target-closed family (previously it was flattened into the base error family).
    sel = browser_engine.REGISTRY.get(STOCK_ENGINE_NAME).select(selection_reason="test")
    assert sel.error_families.target_closed_types == (PlaywrightTargetClosedError,)
    assert sel.target_closed_error_types == (PlaywrightTargetClosedError,)
    # Stock has no dedicated native CDP-transport/retryable class: those families stay empty (bound by
    # a boundary predicate later, not here), never silently merged into base.
    assert sel.error_families.cdp_connection_types == ()
    assert sel.error_families.retryable_types == ()


def test_stock_selection_classifies_native_target_closed_with_full_fidelity():
    # The bug this slice closes: classify must return the specific BrowserTargetClosedError, not a
    # flattened BrowserAutomationError, preserving the native error as __cause__.
    sel = browser_engine.REGISTRY.get(STOCK_ENGINE_NAME).select(selection_reason="test")
    native = PlaywrightTargetClosedError("target page closed")
    classified = sel.classify_error(native)
    assert type(classified) is BrowserTargetClosedError
    assert classified.__cause__ is native


def test_rich_selection_binds_all_families_from_native_identities():
    sel = _rich_selection()
    fam = sel.error_families
    assert fam.timeout_types == (_RichEngineTimeout,)
    assert fam.target_closed_types == (_RichTargetClosed,)
    assert fam.cdp_connection_types == (_RichCdpConnection,)
    assert fam.retryable_types == (_RichRetryable,)
    assert fam.base_error_types == (_RichEngineError,)


def test_rich_selection_classify_preserves_family_precedence():
    # retryable -> target-closed -> timeout -> cdp-connection -> base, exactly as browser_errors defines
    # it; the retryable/CDP pair verifies the most-specific-first ordering under inheritance.
    sel = _rich_selection()
    retry = _RichRetryable("transient disconnect")
    assert type(sel.classify_error(retry)) is BrowserRetryableCdpError
    assert sel.classify_error(retry).__cause__ is retry
    assert type(sel.classify_error(_RichCdpConnection("transport"))) is BrowserCdpConnectionError
    assert type(sel.classify_error(_RichTargetClosed("closed"))) is BrowserTargetClosedError
    assert type(sel.classify_error(_RichEngineTimeout("slow"))) is BrowserTimeoutError
    assert type(sel.classify_error(_RichEngineError("boom"))) is BrowserAutomationError


def test_rich_selection_does_not_classify_foreign_engine_errors():
    # Engine isolation: a rich engine's families must never fire on another engine's native errors,
    # even a foreign target-closed/retryable-looking one; the caller must re-raise (None).
    sel = _rich_selection()
    assert sel.classify_error(_EngineAError("foreign base")) is None
    assert sel.classify_error(_EngineATimeout("foreign timeout")) is None
    assert sel.classify_error(ValueError("unrelated")) is None


def test_spec_rich_loader_is_lazy_and_populates_families_via_select():
    # The rich loader is never called at spec construction (lazy, like _load_error_types) and only
    # runs at select(); the resulting selection carries the richer families.
    calls: list[str] = []

    def _load_rich() -> BrowserEngineRichErrorTypes:
        calls.append("rich")
        return BrowserEngineRichErrorTypes(
            target_closed_types=(_RichTargetClosed,),
            cdp_connection_types=(_RichCdpConnection,),
            retryable_types=(_RichRetryable,),
        )

    spec = BrowserEngineSpec(
        name="engine-rich",
        _start_driver=_never_start,
        _load_error_types=lambda: (_RichEngineError, _RichEngineTimeout),
        _load_rich_error_types=_load_rich,
    )
    assert calls == []  # constructing the spec imported nothing
    sel = spec.select(selection_reason="test")
    assert calls == ["rich"]
    assert sel.error_families.retryable_types == (_RichRetryable,)
    assert sel.error_families.target_closed_types == (_RichTargetClosed,)


def test_spec_without_rich_loader_binds_only_base_and_timeout():
    # Optional empty families: a spec that supplies no rich loader still selects, with the richer
    # families empty (backward-compatible with engines that only expose base+timeout).
    spec = BrowserEngineSpec(
        name="engine-plain",
        _start_driver=_never_start,
        _load_error_types=lambda: (_EngineAError, _EngineATimeout),
    )
    sel = spec.select(selection_reason="test")
    assert sel.error_families.target_closed_types == ()
    assert sel.error_families.cdp_connection_types == ()
    assert sel.error_families.retryable_types == ()
    assert sel.error_families.timeout_types == (_EngineATimeout,)


def test_spec_rich_loader_import_error_fails_closed():
    # A rich loader that cannot import its driver package fails closed at select() (BrowserEngine
    # Unavailable), exactly like the base loader — never a silent fallback to empty families.
    def _load_rich() -> BrowserEngineRichErrorTypes:
        raise ImportError("driver package absent")

    spec = BrowserEngineSpec(
        name="engine-absent-rich",
        _start_driver=_never_start,
        _load_error_types=lambda: (_EngineAError, _EngineATimeout),
        _load_rich_error_types=_load_rich,
    )
    with pytest.raises(BrowserEngineUnavailable):
        spec.select(selection_reason="test")


def test_is_installed_false_when_rich_loader_import_fails_though_base_imports():
    # is_installed() validates BOTH loaders: an engine whose base driver imports but whose richer-family
    # identities do not (e.g. a private target-closed module drifts) is NOT usable — select() would fail
    # closed on that same import — so a default resolver keying on is_installed() must skip it, not pick
    # it and then have select() raise. Base imports fine here; only the rich loader is broken.
    def _load_rich() -> BrowserEngineRichErrorTypes:
        raise ImportError("private rich-family module drifted")

    spec = BrowserEngineSpec(
        name="engine-rich-drift",
        _start_driver=_never_start,
        _load_error_types=lambda: (_EngineAError, _EngineATimeout),
        _load_rich_error_types=_load_rich,
    )
    assert spec.is_installed() is False
    # And the contract stays consistent: explicit selection of the same drifted engine fails loud.
    with pytest.raises(BrowserEngineUnavailable):
        spec.select(selection_reason="explicit")


def test_is_installed_true_when_both_loaders_import_and_stays_lazy_until_probed():
    # With both loaders importable, is_installed() is True. Neither loader runs at construction (lazy);
    # both run only when is_installed() probes them.
    calls: list[str] = []

    def _load_base() -> tuple[type[BaseException], type[BaseException]]:
        calls.append("base")
        return _RichEngineError, _RichEngineTimeout

    def _load_rich() -> BrowserEngineRichErrorTypes:
        calls.append("rich")
        return BrowserEngineRichErrorTypes(target_closed_types=(_RichTargetClosed,))

    spec = BrowserEngineSpec(
        name="engine-both",
        _start_driver=_never_start,
        _load_error_types=_load_base,
        _load_rich_error_types=_load_rich,
    )
    assert calls == []  # construction imported nothing
    assert spec.is_installed() is True
    assert calls == ["base", "rich"]  # is_installed probed both loaders


def test_is_installed_true_with_no_rich_loader():
    # A spec that supplies no rich loader is installed as long as its base loader imports (backward
    # compatible with engines that only expose base+timeout).
    spec = BrowserEngineSpec(
        name="engine-base-only",
        _start_driver=_never_start,
        _load_error_types=lambda: (_EngineAError, _EngineATimeout),
    )
    assert spec.is_installed() is True


def test_stock_spec_rich_loader_import_is_lazy_not_module_level():
    # Regression: the stock target-closed identity is imported inside the rich loader, not at module
    # scope, so importing browser_engine never resolves the private playwright._impl._errors module.
    source = Path(browser_engine.__file__).read_text()
    assert "_PlaywrightTargetClosedError" not in source
    assert "from playwright._impl._errors import" in source  # present, but only inside the loader body
    # The installed stock spec still resolves the identity when probed/selected.
    assert browser_engine.PLAYWRIGHT_SPEC.is_installed() is True
    sel = browser_engine.PLAYWRIGHT_SPEC.select(selection_reason="test")
    assert sel.error_families.target_closed_types == (PlaywrightTargetClosedError,)


def test_stock_selection_degrades_when_private_target_closed_import_moves(monkeypatch):
    # The stock engine is the always-on default path (OSS default resolver + cloud last-resort
    # fallback), so a Playwright bump that moves the PRIVATE playwright._impl._errors module must NOT
    # make stock globally unavailable. Simulate the module being gone: select() still succeeds, the
    # stable public base+timeout stay bound, and the target-closed family degrades to empty (best
    # effort) instead of raising BrowserEngineUnavailable.
    monkeypatch.setitem(sys.modules, "playwright._impl._errors", None)
    assert browser_engine.PLAYWRIGHT_SPEC.is_installed() is True
    sel = browser_engine.PLAYWRIGHT_SPEC.select(selection_reason="test")
    assert sel.name == STOCK_ENGINE_NAME
    assert sel.error_families.target_closed_types == ()
    assert sel.error_families.timeout_types == (PlaywrightTimeoutError,)
    assert sel.error_families.base_error_types == (PlaywrightError,)


@pytest.mark.asyncio
async def test_default_resolver_survives_private_target_closed_import_move(monkeypatch):
    # The OSS default resolver calls stock select() UNGUARDED by is_installed(); a moved private
    # target-closed module must not turn every default run into BrowserEngineUnavailable.
    monkeypatch.setitem(sys.modules, "playwright._impl._errors", None)
    sel = await resolve_browser_engine(BrowserEngineContext(browser_source="local-browser"))
    assert sel.name == STOCK_ENGINE_NAME
    assert sel.error_families.target_closed_types == ()


def test_nonstock_rich_loader_stays_fail_loud_when_its_import_moves():
    # Degradation is stock-SPECIFIC: a non-stock engine whose rich loader raises ImportError still
    # fails closed (is_installed False + select raises), so an explicitly selected or misconfigured
    # engine never silently loses fidelity. This guards against broadening the stock best-effort path.
    def _load_rich() -> BrowserEngineRichErrorTypes:
        raise ImportError("private module moved")

    spec = BrowserEngineSpec(
        name="engine-nonstock",
        _start_driver=_never_start,
        _load_error_types=lambda: (_EngineAError, _EngineATimeout),
        _load_rich_error_types=_load_rich,
    )
    assert spec.is_installed() is False
    with pytest.raises(BrowserEngineUnavailable):
        spec.select(selection_reason="explicit")


def test_rich_family_type_outside_base_family_rejects_construction():
    # Fail-loud invariant: a richer-family native type that is NOT a subclass of the engine base error
    # would let classification fire on a foreign error; reject it at construction.
    with pytest.raises(BrowserErrorFamiliesConfigError):
        _selection("engine-bad", _RichEngineError, _RichEngineTimeout, target_closed_types=(_EngineAError,))


def test_rich_family_type_duplicated_across_families_rejects_construction():
    # A native type may belong to only one family; binding the same class as both CDP-connection and
    # retryable must fail loudly (enforced by BrowserEngineErrorFamilies, surfaced at selection build).
    with pytest.raises(BrowserErrorFamiliesConfigError):
        _selection(
            "engine-dup",
            _RichEngineError,
            _RichEngineTimeout,
            cdp_connection_types=(_RichCdpConnection,),
            retryable_types=(_RichCdpConnection,),
        )


def test_rich_family_type_colliding_with_base_rejects_construction():
    # Binding the engine base error itself into a richer family collides with the derived base family;
    # reject rather than silently shadow the base-error classification.
    with pytest.raises(BrowserErrorFamiliesConfigError):
        _selection("engine-base-collide", _RichEngineError, _RichEngineTimeout, target_closed_types=(_RichEngineError,))


async def _ok_start():
    return object()


def _async(value):
    async def _coro(ctx=None):
        return value

    return _coro()
