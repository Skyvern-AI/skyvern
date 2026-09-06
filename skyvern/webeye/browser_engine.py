"""Per-run browser-engine selection seam (OSS foundation).

Model: one worker image can ship more than one browser-driver engine (stock Playwright always;
Rustwright when the image installs it; a cloud-private engine registered by the cloud layer). The
engine is chosen PER RUN — resolved once at the run's browser-ownership boundary and pinned to that
run's browser resources for their whole lifetime (create -> reconnect). There is no process-global
"active engine": two concurrent runs in one process can pin different engines with no shared mutable
state, so one run's engine (or its exception identity) can never change underneath another.

The same image holds a small registry of engine *specs*; a resolver picks one spec per run and
materializes it into an immutable ``BrowserEngineSelection`` that carries the driver factory, the
driver's public exception identity, and its capability metadata. Constructing a spec never imports
the driver package, so a spec for an engine this image does not ship is inert until a run selects
it; selecting an engine whose package is absent fails closed (``BrowserEngineUnavailable``) rather
than silently falling back.

This is the neutral foundation only. The cloud resolver, the cloud-private engine, and the
deployment/image changes that would let one image and one queue interleave all engines live in
later, stacked changes; the legacy build-time source rewrite (``scripts/patch_browser.sh``) still
ships until that slice lands.
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, TypeAlias, cast

import structlog
from playwright.async_api import Error as _PlaywrightError
from playwright.async_api import Playwright
from playwright.async_api import TimeoutError as _PlaywrightTimeoutError
from playwright.async_api import async_playwright

from skyvern.exceptions import SkyvernException
from skyvern.webeye.browser_errors import (
    BrowserAutomationError,
    BrowserEngineErrorFamilies,
    BrowserErrorFamiliesConfigError,
    classify_browser_error,
)
from skyvern.webeye.driver_connection import close_driver_connection_on_transport_loss

if TYPE_CHECKING:
    from playwright.async_api import Frame as _StockFrame

    from skyvern.forge.sdk.schemas.tasks import Task
    from skyvern.webeye.browser_manager import BrowserManager
    from skyvern.webeye.skycdp.facade.page import Frame as _SkyCdpFrame

    # Callers annotate against this rather than naming an engine's frame class, so engine identity
    # stays behind the registry seam that test_experimental_engine_production_isolation pins.
    EngineFrame: TypeAlias = "_StockFrame | _SkyCdpFrame"

LOG = structlog.get_logger()

BrowserDriverStarter = Callable[[], Awaitable[Playwright]]
BrowserEngineErrorLoader = Callable[[], "tuple[type[BaseException], type[BaseException]]"]
BrowserEngineRichErrorLoader = Callable[[], "BrowserEngineRichErrorTypes"]
UNSET_SELECTION = cast("BrowserEngineSelection | None", object())

STOCK_ENGINE_NAME = "playwright"
RUSTWRIGHT_ENGINE_NAME = "rustwright"
SKYCDP_ENGINE_NAME = "skycdp"

# skycdp is an in-tree, attach-only raw-CDP driver: no driver subprocess, no bundled browser, no
# launcher. It arrives DENY-ALL for the same reason Rustwright does — a driver swap is a behavioural
# risk, not a config change, and the measured conformance surface is still narrower than the
# Playwright surface production uses. A source is carved in only once its behaviours are covered by
# tests/browser_e2e/test_skycdp_conformance.py.
SKYCDP_ALLOWED_BROWSER_SOURCES: frozenset[str] = frozenset()

# Rustwright is registered so the adapter contract exists, but it is DENY-ALL (empty capability set)
# and thus not rollout-capable: no production browser source may select it. Paths it would serve
# still carry stock-Playwright ``except`` clauses (e.g. the cloud browser factory), so a Rustwright
# error would bypass those recovery branches; every source stays denied until per-run exception
# normalization and concrete source attribution land. Pinned by a test (SKY-12007 follow-up).
RUSTWRIGHT_ALLOWED_BROWSER_SOURCES: frozenset[str] = frozenset()


def resolve_engine_version(package_name: str) -> str | None:
    """Best-effort installed version of a driver package, or None when its metadata is absent."""
    try:
        return importlib.metadata.version(package_name)
    except importlib.metadata.PackageNotFoundError:
        return None


class UnknownBrowserEngine(SkyvernException):
    """Raised when a run selects an engine name that is not in the registry (fail closed)."""

    def __init__(self, name: str, known: tuple[str, ...]) -> None:
        self.name = name
        super().__init__(f"Unknown browser engine {name!r}; registered engines are {sorted(known)!r}")


class BrowserEngineUnavailable(SkyvernException):
    """Raised when a selected engine's driver package is not installed in this image (fail closed)."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Browser engine {name!r} is registered but its driver package is not installed")


class BrowserEngineBootstrapError(SkyvernException):
    """A selected driver could not launch a usable browser context (a boot failure before any usable
    context exists). Raised by a launch seam to mark a narrow experiment-engine launch failure that the
    acquisition boundary may degrade once to the selection's classical boot fallback."""


class BrowserSourceNotSupportedByEngine(SkyvernException):
    """Raised when a resolved browser source is outside the selected engine's capability set."""

    def __init__(self, *, browser_source: str | None, engine: str) -> None:
        self.browser_source = browser_source
        self.engine = engine
        described = repr(browser_source) if browser_source is not None else "an unattributed browser source (None)"
        super().__init__(f"Browser source {described} is not supported by the selected browser engine {engine!r}")


@dataclass(frozen=True)
class BrowserEngineMetadata:
    """Attribution + capability for a selected engine.

    ``allowed_browser_sources`` is the fail-closed capability: ``None`` means unrestricted (every
    source allowed — the unchanged default), a set restricts the engine to exactly those neutral
    browser-source strings, and the empty set denies every source (a registered-but-not-rollout-able
    engine).
    """

    name: str
    version: str | None = None
    allowed_browser_sources: frozenset[str] | None = None

    def allows(self, browser_source: str | None) -> bool:
        return self.allowed_browser_sources is None or browser_source in self.allowed_browser_sources


@dataclass(frozen=True)
class BrowserEngineRichErrorTypes:
    """Native identities for the richer taxonomy families a driver package exposes, beyond the
    base+timeout pair every driver has. A spec's rich loader imports these lazily from the driver
    package (never at spec construction); each family is optional — an empty tuple means the engine
    exposes no distinct native class for it (its detection, if any, is left to a predicate-based
    boundary), never a silent downgrade. Carried onto the selection so ``classify_error`` can preserve
    target-closed / CDP-transport / retryable distinctions instead of flattening them into the base.
    """

    target_closed_types: tuple[type[BaseException], ...] = ()
    cdp_connection_types: tuple[type[BaseException], ...] = ()
    retryable_types: tuple[type[BaseException], ...] = ()


@dataclass(frozen=True)
class BrowserEngineSelection:
    """A single run's pinned engine. Immutable; created once and carried through the browser
    resource's whole lifetime. Concurrent runs hold distinct selections with no shared state, so
    an engine — and its exception identity — cannot change underneath a live browser resource.
    """

    name: str
    start_driver: BrowserDriverStarter
    error_type: type[BaseException]
    timeout_error_type: type[BaseException]
    metadata: BrowserEngineMetadata
    selection_reason: str
    # The richer taxonomy families' native identities, loaded lazily by ``select()`` from the driver
    # package alongside the base+timeout pair. Each defaults empty: an engine that exposes no distinct
    # native class for a family (e.g. stock Playwright has no dedicated CDP-transport class) simply
    # leaves it unbound. A directly-constructed selection with none of these still binds base+timeout.
    target_closed_error_types: tuple[type[BaseException], ...] = ()
    cdp_connection_error_types: tuple[type[BaseException], ...] = ()
    retryable_error_types: tuple[type[BaseException], ...] = ()
    # One-shot classical fallback used ONLY if THIS selection's driver fails to boot at the run's
    # browser-ownership boundary, before any browser/page resource exists (see
    # ``RealBrowserManager._create_browser_state``). ``None`` (the default, and what ``select()`` always
    # produces) means no fallback — a boot failure fails the run as before. The fallback itself carries
    # no further fallback, so the degrade is exactly one hop and never fires after a browser side effect.
    boot_fallback_selection: BrowserEngineSelection | None = None
    # Derived once from the identities above (which ``select()`` loaded lazily from the driver
    # package), never passed in: the immutable error-family binding that ``classify_error`` uses. The
    # base+timeout pair is the stable public identity every driver exposes; the richer families are
    # populated only for the native classes the selected engine actually exposes.
    error_families: BrowserEngineErrorFamilies = field(init=False)

    def __post_init__(self) -> None:
        # Invariant: the timeout identity must be the base identity or a subclass of it, so a timeout
        # is always an engine error and classification's timeout-before-base precedence holds. A
        # reverse hierarchy (base subclasses timeout) would classify plain base errors as timeouts and
        # break is_engine_error/is_engine_timeout_error semantics; unrelated identities would leave a
        # timeout outside the engine's error family. Fail loudly rather than bind a misleading engine.
        if not issubclass(self.timeout_error_type, self.error_type):
            raise BrowserErrorFamiliesConfigError(
                f"timeout identity {self.timeout_error_type.__name__} must be {self.error_type.__name__} or a "
                f"subclass of it; got an incompatible hierarchy for engine {self.name!r}"
            )
        # Invariant: every richer-family native identity must itself be a subclass of the engine's base
        # error, so a positive classification implies is_engine_error — the selection can never match a
        # foreign engine's class into one of its own families. A richer type outside the base family
        # would let classification fire on an error this engine did not raise (foreign-engine false
        # classification); fail loudly rather than bind it. Stdlib/transport errors that are not engine
        # errors belong to a predicate at the boundary, not to these native-identity bindings.
        for family_name, types in (
            ("target_closed_error_types", self.target_closed_error_types),
            ("cdp_connection_error_types", self.cdp_connection_error_types),
            ("retryable_error_types", self.retryable_error_types),
        ):
            for entry in types:
                if not (isinstance(entry, type) and issubclass(entry, self.error_type)):
                    raise BrowserErrorFamiliesConfigError(
                        f"{family_name} entry {entry!r} must be a subclass of the engine's base error "
                        f"{self.error_type.__name__} for engine {self.name!r}"
                    )
        # A real driver's timeout is a distinct subclass of its base error, so both identities occupy
        # separate families. Only when an engine reports the same class for both (base is the timeout)
        # is the base entry dropped: the class already appears in the more-specific timeout family,
        # which classification checks first, and a native type may live in only one family. The richer
        # families' one-type-per-family / disjointness checks are enforced by BrowserEngineErrorFamilies.
        base_error_types = () if self.error_type is self.timeout_error_type else (self.error_type,)
        object.__setattr__(
            self,
            "error_families",
            BrowserEngineErrorFamilies(
                timeout_types=(self.timeout_error_type,),
                target_closed_types=self.target_closed_error_types,
                cdp_connection_types=self.cdp_connection_error_types,
                retryable_types=self.retryable_error_types,
                base_error_types=base_error_types,
            ),
        )

    def is_engine_error(self, exc: BaseException) -> bool:
        """True if ``exc`` is a driver-family error from THIS run's engine (adapter-bound identity)."""
        return isinstance(exc, self.error_type)

    def is_engine_timeout_error(self, exc: BaseException) -> bool:
        return isinstance(exc, self.timeout_error_type)

    def classify_error(self, exc: BaseException) -> BrowserAutomationError | None:
        """Map a native error using this run's selected-engine families, returning ``None`` for
        foreign/unknown errors; positive classifications preserve the native error as ``__cause__``.
        Only populated families are reachable, so callers must branch on the returned taxonomy type
        rather than assume support for any specific family."""
        return classify_browser_error(exc, self.error_families)

    def ensure_supports(self, browser_source: str | None) -> None:
        """Fail closed before any provisioning if this engine cannot serve ``browser_source``.

        A restricted engine (non-``None`` ``allowed_browser_sources``, including the deny-all empty
        set) also rejects an unattributed (``None``) source: the fail-closed capability treats an
        unknown source as the most suspicious input, never as implicitly allowed.
        """
        if not self.metadata.allows(browser_source):
            raise BrowserSourceNotSupportedByEngine(browser_source=browser_source, engine=self.name)

    def attribution(self) -> dict[str, str | None]:
        """Structured attribution for logging every run/browser creation — no hidden fallback."""
        return {
            "browser_engine": self.name,
            "browser_engine_version": self.metadata.version,
            "browser_engine_selection_reason": self.selection_reason,
        }


def resolve_engine_selection_for_task(
    task: Task | None, browser_manager: BrowserManager
) -> BrowserEngineSelection | None:
    if task is None:
        return None
    browser_state = browser_manager.get_for_task(task.task_id, workflow_run_id=task.workflow_run_id)
    return browser_state.engine_selection if browser_state is not None else None


@dataclass(frozen=True)
class BrowserEngineSpec:
    """Immutable registry entry describing how to materialize one engine.

    Constructing a spec never imports the driver package, so a spec for an engine this image does
    not ship is inert until a run actually selects it. ``select()`` is the only place the driver's
    error classes are imported; an absent package fails closed there.
    """

    name: str
    _start_driver: BrowserDriverStarter
    _load_error_types: BrowserEngineErrorLoader
    allowed_browser_sources: frozenset[str] | None = None
    # Optional lazy loader for the richer taxonomy families' native identities. ``None`` means the
    # engine binds only base+timeout (the richer families stay empty). Like ``_load_error_types`` it
    # is never called at spec construction, so a spec for an absent engine stays inert until selected.
    _load_rich_error_types: BrowserEngineRichErrorLoader | None = None

    def is_installed(self) -> bool:
        # Validate BOTH loaders under the one installed/unavailable contract: an engine whose base
        # driver imports but whose richer-family identities do not (e.g. a private target-closed module
        # drifts) is NOT usable, because select() would fail closed on that same import. Reporting it
        # installed would let a default resolver pick it and then have select() raise; validating both
        # here keeps is_installed() and select() consistent so the default falls back instead.
        try:
            self._load_error_types()
            if self._load_rich_error_types is not None:
                self._load_rich_error_types()
            return True
        except ImportError:
            return False

    def select(self, *, selection_reason: str) -> BrowserEngineSelection:
        try:
            error_type, timeout_error_type = self._load_error_types()
            rich = self._load_rich_error_types() if self._load_rich_error_types is not None else None
        except ImportError as exc:
            raise BrowserEngineUnavailable(self.name) from exc
        rich = rich if rich is not None else BrowserEngineRichErrorTypes()
        return BrowserEngineSelection(
            name=self.name,
            start_driver=self._start_driver,
            error_type=error_type,
            timeout_error_type=timeout_error_type,
            target_closed_error_types=rich.target_closed_types,
            cdp_connection_error_types=rich.cdp_connection_types,
            retryable_error_types=rich.retryable_types,
            metadata=BrowserEngineMetadata(
                name=self.name,
                version=resolve_engine_version(self.name),
                allowed_browser_sources=self.allowed_browser_sources,
            ),
            selection_reason=selection_reason,
        )


@dataclass
class BrowserEngineRegistry:
    """Catalog of engine specs, populated once at startup (import time for OSS engines; cloud
    registers its private engine when ``cloud`` is imported). It holds no *active* engine — there is
    none — so a run reads it but nothing here is per-run mutable state. Registration is not
    thread-safe and is expected to complete during single-threaded startup before any run resolves.
    """

    _specs: dict[str, BrowserEngineSpec] = field(default_factory=dict)

    def register(self, spec: BrowserEngineSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Browser engine {spec.name!r} is already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> BrowserEngineSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise UnknownBrowserEngine(name, tuple(self._specs)) from None

    def names(self) -> frozenset[str]:
        return frozenset(self._specs)


async def _start_stock_driver() -> Playwright:
    driver = await async_playwright().start()
    close_driver_connection_on_transport_loss(driver)
    return driver


def _stock_error_types() -> tuple[type[BaseException], type[BaseException]]:
    return _PlaywrightError, _PlaywrightTimeoutError


def _stock_rich_error_types() -> BrowserEngineRichErrorTypes:
    # Stock Playwright exposes a distinct native target-closed class; it has no dedicated native class
    # for CDP-transport or retryable failures (those are message/stdlib-shaped and detected by a
    # predicate at the boundary, not bound here), so those families stay empty. The target-closed class
    # lives in a PRIVATE module (playwright._impl._errors) with no API-stability guarantee, imported
    # lazily here so it never resolves at module import.
    #
    # Stock is the always-on default path (the OSS default resolver selects it unguarded, and cloud
    # falls back to it as last resort), so its richer-family binding must never make stock globally
    # unavailable. Target-closed detection is additive/best-effort, so if a future Playwright bump
    # moves or drops that private module we degrade to empty rich families (base+timeout, from the
    # stable public surface, still bind) with a warning — rather than fail select() and break browser
    # creation for every run. The except is narrow (ImportError from this one optional import); it is
    # stock-specific and does NOT relax the fail-loud contract for non-stock/explicitly-selected engines.
    try:
        from playwright._impl._errors import TargetClosedError as PlaywrightTargetClosedError
    except ImportError:
        LOG.warning(
            "playwright_private_target_closed_import_unavailable",
            detail=(
                "playwright._impl._errors.TargetClosedError could not be imported (likely a Playwright "
                "upgrade moved it); stock engine binds no target-closed family this run. Base and "
                "timeout families are unaffected."
            ),
        )
        return BrowserEngineRichErrorTypes()
    return BrowserEngineRichErrorTypes(target_closed_types=(PlaywrightTargetClosedError,))


async def _start_rustwright_driver() -> Playwright:
    from rustwright.async_api import async_playwright as rustwright_async_playwright

    driver = await rustwright_async_playwright().start()
    close_driver_connection_on_transport_loss(driver)
    return driver


def _rustwright_error_types() -> tuple[type[BaseException], type[BaseException]]:
    from rustwright.async_api import Error as RustwrightError
    from rustwright.async_api import TimeoutError as RustwrightTimeoutError

    return RustwrightError, RustwrightTimeoutError


def _rustwright_rich_error_types() -> BrowserEngineRichErrorTypes:
    # Rustwright (evaluated at rev f4d8091 / 0.1.0a2) exposes a public ``TargetClosedError`` that
    # subclasses its base ``Error`` and is disjoint from ``TimeoutError`` — same shape as stock. Like
    # stock it exposes no distinct native CDP-transport or retryable class (its dead-port connect
    # errors land on the base ``Error`` family), so those stay empty. Imported lazily: this is never
    # called until a run selects rustwright, so an image lacking the package stays inert.
    from rustwright.async_api import TargetClosedError as RustwrightTargetClosedError

    return BrowserEngineRichErrorTypes(target_closed_types=(RustwrightTargetClosedError,))


async def _start_skycdp_driver() -> Playwright:
    from skyvern.webeye.skycdp import async_skycdp

    # Structurally a Playwright driver for the one surface a CDP-connect run uses
    # (``chromium.connect_over_cdp``); it is not a playwright.Playwright, and cannot be.
    return cast(Playwright, await async_skycdp().start())


def _skycdp_error_types() -> tuple[type[BaseException], type[BaseException]]:
    from skyvern.webeye.skycdp.errors import CdpError, CdpTimeoutError

    return CdpError, CdpTimeoutError


def _skycdp_rich_error_types() -> BrowserEngineRichErrorTypes:
    from skyvern.webeye.skycdp.errors import CdpConnectionError, CdpTargetClosedError

    return BrowserEngineRichErrorTypes(
        target_closed_types=(CdpTargetClosedError,),
        cdp_connection_types=(CdpConnectionError,),
    )


PLAYWRIGHT_SPEC = BrowserEngineSpec(
    name=STOCK_ENGINE_NAME,
    _start_driver=_start_stock_driver,
    _load_error_types=_stock_error_types,
    allowed_browser_sources=None,
    _load_rich_error_types=_stock_rich_error_types,
)

RUSTWRIGHT_SPEC = BrowserEngineSpec(
    name=RUSTWRIGHT_ENGINE_NAME,
    _start_driver=_start_rustwright_driver,
    _load_error_types=_rustwright_error_types,
    allowed_browser_sources=RUSTWRIGHT_ALLOWED_BROWSER_SOURCES,
    _load_rich_error_types=_rustwright_rich_error_types,
)

SKYCDP_SPEC = BrowserEngineSpec(
    name=SKYCDP_ENGINE_NAME,
    _start_driver=_start_skycdp_driver,
    _load_error_types=_skycdp_error_types,
    allowed_browser_sources=SKYCDP_ALLOWED_BROWSER_SOURCES,
    _load_rich_error_types=_skycdp_rich_error_types,
)

REGISTRY = BrowserEngineRegistry()
REGISTRY.register(PLAYWRIGHT_SPEC)
REGISTRY.register(RUSTWRIGHT_SPEC)
REGISTRY.register(SKYCDP_SPEC)


@dataclass(frozen=True)
class BrowserEngineContext:
    """Per-run inputs a resolver may use to pick and validate an engine. Available at the browser
    resource's creation site; ``browser_source`` is the neutral provider/source string the resolver
    validates against the engine's capability set."""

    organization_id: str | None = None
    workflow_run_id: str | None = None
    workflow_permanent_id: str | None = None
    task_id: str | None = None
    script_id: str | None = None
    # The canonical key this run's selection is pinned/owned under (set by get_or_resolve_engine_selection).
    # A resolver keys the flag on this so every resource in one run resolves identically even when a caller
    # deliberately leaves workflow_run_id unset here (task-first creation keeps its download-dir scoping).
    run_key: str | None = None
    browser_source: str | None = None


BrowserEngineResolver = Callable[[BrowserEngineContext], Awaitable[BrowserEngineSelection]]


async def _default_resolver(context: BrowserEngineContext) -> BrowserEngineSelection:
    """OSS default: stock Playwright, unconditionally. Cloud overrides this to read the multivariate
    engine flag (see ``cloud.webeye.browser_engine``)."""
    return REGISTRY.get(STOCK_ENGINE_NAME).select(selection_reason="oss-default-playwright")


_resolver: BrowserEngineResolver = _default_resolver


def set_browser_engine_resolver(resolver: BrowserEngineResolver) -> None:
    """Install the per-run resolver strategy (cloud does this at startup). This injects HOW an engine
    is chosen; it does not hold a chosen engine, so it is not process-global active-engine state."""
    global _resolver
    _resolver = resolver


def reset_browser_engine_resolver() -> None:
    """Restore the OSS default resolver (used by tests)."""
    global _resolver
    _resolver = _default_resolver


async def resolve_browser_engine(context: BrowserEngineContext) -> BrowserEngineSelection:
    """Resolve an engine for ``context`` via the installed resolver, then validate the run's browser
    source against the engine's capability set (fail closed before any provisioning). Callers that
    own a logical run resolve once and reuse the returned selection for every browser resource in
    that run (see ``RealBrowserManager``), so the engine is pinned per run, not per resource.

    An unrestricted engine (``allowed_browser_sources is None``) may resolve with no attributed
    source; a restricted engine fails closed on an unattributed source rather than passing the gate."""
    selection = await _resolver(context)
    selection.ensure_supports(context.browser_source)
    return selection
