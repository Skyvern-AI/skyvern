"""Per-run browser-engine selection (OSS seam).

These tests stay driver-agnostic: they exercise the registry, per-run selection, capability gate,
and exception-identity classification with fake engine specs, so they hold on an image that ships
only stock Playwright. Cloud-only concerns (the cloud-private engine, the multivariate flag) are
left to the cloud wiring slice that introduces them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.webeye import browser_engine
from skyvern.webeye.browser_engine import (
    RUSTWRIGHT_ALLOWED_BROWSER_SOURCES,
    STOCK_ENGINE_NAME,
    BrowserEngineContext,
    BrowserEngineMetadata,
    BrowserEngineRegistry,
    BrowserEngineSelection,
    BrowserEngineSpec,
    BrowserSourceNotSupportedByEngine,
    UnknownBrowserEngine,
    resolve_browser_engine,
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
) -> BrowserEngineSelection:
    return BrowserEngineSelection(
        name=name,
        start_driver=start,
        error_type=error_type,
        timeout_error_type=timeout_type,
        metadata=BrowserEngineMetadata(name=name, version="0.0.0", allowed_browser_sources=allowed_sources),
        selection_reason="test",
    )


@pytest.fixture(autouse=True)
def _restore_resolver():
    yield
    browser_engine.reset_browser_engine_resolver()


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
    # rustwright is not installed in the OSS test image: selecting it must fail closed, never fall back.
    spec = browser_engine.REGISTRY.get(browser_engine.RUSTWRIGHT_ENGINE_NAME)
    assert spec.is_installed() is False
    with pytest.raises(browser_engine.BrowserEngineUnavailable):
        spec.select(selection_reason="explicit-rustwright")


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


async def _ok_start():
    return object()


def _async(value):
    async def _coro(ctx=None):
        return value

    return _coro()
