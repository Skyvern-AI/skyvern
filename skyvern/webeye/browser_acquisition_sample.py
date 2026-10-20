"""First-try fields for the canonical browser acquisition event.

The canonical acquisition pipeline (``browser_runtime_events.log_browser_runtime_event`` with
``event="acquire_result"``) owns the terminal outcome. This module contributes the *first-try*
enrichment for ``acquire_mode="create"`` acquisitions so a "% created on the first try" ratio
(sessions, not runs) can be computed downstream from that one event — without a second pipeline.

A run-scoped accumulator, owned at the acquisition boundary (``RealBrowserManager._create_browser_state``,
spanning the engine boot-fallback retry), collects the retry counts and any cross-family fallback. The
provider-create and CDP-connect retry sites record into it. ``first_try_success`` is derived here, in
application code, from the canonical outcome plus those counters, so a downstream dashboard never
reinterprets them. This module never emits a log itself.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

FIRST_TRY_SCHEMA_VERSION = 1

# The vocabulary of ``workflow_runs.browser_runtime``: a run bound to a persistent session is ``pbs``
# whoever operates that session's browser, ``vendor`` is a third-party browser created for the run.
BrowserRuntime = Literal["local", "pbs", "vendor"]
BROWSER_RUNTIMES: frozenset[str] = frozenset(get_args(BrowserRuntime))

# Cross-family fallback is a bounded enum, never a local-only boolean: a run that stayed on its
# requested family, one that degraded to a local browser, and one that degraded to a browser
# engine's classical boot fallback are distinct populations, and each defeats first-try success.
FALLBACK_TARGET_NONE = "none"
FALLBACK_TARGET_LOCAL = "local"
FALLBACK_TARGET_CLASSICAL_ENGINE = "classical_engine"
FALLBACK_TARGETS = frozenset({FALLBACK_TARGET_NONE, FALLBACK_TARGET_LOCAL, FALLBACK_TARGET_CLASSICAL_ENGINE})

ACQUIRE_MODE_CREATE = "create"
ACQUIRE_MODE_ATTACH = "attach"
_RESOLVABLE_ACQUIRE_MODES = frozenset({ACQUIRE_MODE_CREATE, ACQUIRE_MODE_ATTACH})

_MAX_STR_LEN = 64


@dataclass
class BrowserAcquisitionSample:
    """Accumulated observations for one browser-session acquisition attempt.

    ``provider_create_attempts`` / ``cdp_connect_attempts`` count application-level attempts at
    each stage; the first attempt is 1, so a retry count is ``max(0, attempts - 1)``.

    A zero ``provider_create_attempts`` means no *instrumented* application-level provider create
    ran: a local launch performs none, and a vendor family that creates its session implicitly at
    the CDP dial (BrightData, UndetectIO) has no separate throttled create — its only
    application-level retry is the CDP connect, counted in ``cdp_connect_attempts``. It never means
    SDK-internal HTTP retries were zero; those are deliberately folded into the first
    application-level invocation and not instrumented. (Browserbase is not in this taxonomy: its
    session is provisioned earlier, outside this sample, and its connector attaches — see
    cloud_browser_factory's Browserbase dispatch branch.)
    """

    requested_family: str | None = None
    effective_family: str | None = None
    provider_create_attempts: int = 0
    cdp_connect_attempts: int = 0
    fallback_target: str = FALLBACK_TARGET_NONE
    failure_stage: str | None = None
    # The acquisition mode resolved at dispatch time. A vendor branch that provisions a new session
    # while ignoring a supplied fallback browser_address marks ``create`` here before its first
    # attempt, so both the success and the terminal failure/cancellation canonical events resolve to
    # the real mode rather than the pre-dispatch address heuristic. ``None`` = fall back to that heuristic.
    resolved_acquire_mode: str | None = None
    # Where the dispatched creator runs the browser, and who operates it when that is not this process.
    browser_runtime: BrowserRuntime | None = None
    browser_vendor: str | None = None
    closed: bool = False


_current_sample: contextvars.ContextVar[BrowserAcquisitionSample | None] = contextvars.ContextVar(
    "browser_acquisition_sample", default=None
)


def begin_browser_acquisition_sample(requested_family: str | None = None) -> contextvars.Token:
    """Open and own an acquisition scope. Always allocates a fresh sample: ``ContextVar.set``
    affects only the current context, so an independent child task — which inherits the parent's
    ContextVar reference — that begins its own acquisition gets its own sample and never
    overwrites the parent's. The owner passes the returned token to
    ``close_browser_acquisition_sample``."""
    return _current_sample.set(BrowserAcquisitionSample(requested_family=_bounded(requested_family)))


def current_browser_acquisition_sample() -> BrowserAcquisitionSample | None:
    sample = _current_sample.get()
    if sample is None or sample.closed:
        return None
    return sample


def _bounded(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:_MAX_STR_LEN] if text else None


def note_requested_family(family: str | None) -> None:
    sample = current_browser_acquisition_sample()
    if sample is not None:
        sample.requested_family = _bounded(family)


def note_effective_family(family: str | None) -> None:
    sample = current_browser_acquisition_sample()
    if sample is not None:
        sample.effective_family = _bounded(family)


def note_provider_create_attempts(attempts: int) -> None:
    """Record provider/session-create attempts (monotonic max across the acquisition, so a
    vendor's attempts survive a later cross-family fallback that performs no provider create)."""
    sample = current_browser_acquisition_sample()
    if sample is not None and isinstance(attempts, int) and attempts > sample.provider_create_attempts:
        sample.provider_create_attempts = attempts


def note_cdp_connect_attempts(attempts: int) -> None:
    sample = current_browser_acquisition_sample()
    if sample is not None and isinstance(attempts, int) and attempts > sample.cdp_connect_attempts:
        sample.cdp_connect_attempts = attempts


def note_fallback_target(target: str) -> None:
    """Mark a cross-family degrade. Clears any failure stage recorded by the abandoned stage so a
    later failure on the fallback path is not mislabeled with the prior stage's classification."""
    sample = current_browser_acquisition_sample()
    if sample is not None and target in FALLBACK_TARGETS:
        sample.fallback_target = target
        sample.failure_stage = None


def note_failure_stage(stage: str | None) -> None:
    sample = current_browser_acquisition_sample()
    if sample is not None and stage:
        sample.failure_stage = str(stage)[:_MAX_STR_LEN]


def note_resolved_acquire_mode(mode: str) -> None:
    """Record the acquisition mode resolved at dispatch time (before the first create attempt). A
    vendor branch that provisions a new session ignoring a fallback ``browser_address`` marks
    ``create``; ``create`` is sticky, so a later cross-family fallback that dials an address cannot
    downgrade the acquisition back to ``attach``. No-op without an open scope."""
    sample = current_browser_acquisition_sample()
    if sample is None or mode not in _RESOLVABLE_ACQUIRE_MODES:
        return
    if sample.resolved_acquire_mode != ACQUIRE_MODE_CREATE:
        sample.resolved_acquire_mode = mode


def note_browser_runtime(runtime: str, vendor: str | None = None) -> None:
    """Record the runtime of the creator this acquisition dispatched to; the latest creator wins, so a
    vendor-to-local degrade reports local. ``pbs`` is sticky, matching the run-level tag: a creator
    that is not handed the session id still runs the session's browser."""
    sample = current_browser_acquisition_sample()
    if sample is None or runtime not in BROWSER_RUNTIMES:
        return
    if sample.browser_runtime != "pbs":
        sample.browser_runtime = cast(BrowserRuntime, runtime)
    sample.browser_vendor = _bounded(vendor)


def acquired_browser_runtime() -> tuple[BrowserRuntime, str | None] | None:
    sample = current_browser_acquisition_sample()
    if sample is None or sample.browser_runtime is None:
        return None
    return sample.browser_runtime, sample.browser_vendor


def resolve_acquire_mode(default: str) -> str:
    """The dispatch-resolved acquisition mode for the active acquisition, or ``default`` (the
    pre-dispatch address heuristic) when nothing resolved it."""
    sample = current_browser_acquisition_sample()
    if sample is not None and sample.resolved_acquire_mode is not None:
        return sample.resolved_acquire_mode
    return default


def _retries(attempts: int) -> int:
    return attempts - 1 if attempts > 1 else 0


def is_first_try_success(sample: BrowserAcquisitionSample, outcome_success: bool) -> bool:
    return (
        outcome_success
        and sample.fallback_target == FALLBACK_TARGET_NONE
        and _retries(sample.provider_create_attempts) == 0
        and _retries(sample.cdp_connect_attempts) == 0
    )


def acquire_first_try_fields(*, outcome_success: bool) -> dict[str, Any]:
    """The first-try enrichment for the canonical ``acquire_result`` event, or ``{}`` when no
    acquisition scope is open (attach/reuse, mid-run reconnects, direct callers). Gated on
    ``outcome_success`` so a failed or canceled create can never read as a first-try success."""
    sample = current_browser_acquisition_sample()
    if sample is None:
        return {}
    return {
        "first_try_schema_version": FIRST_TRY_SCHEMA_VERSION,
        "first_try_success": is_first_try_success(sample, outcome_success),
        "provider_create_retry_count": _retries(sample.provider_create_attempts),
        "cdp_connect_retry_count": _retries(sample.cdp_connect_attempts),
        "fallback_target": sample.fallback_target,
        "requested_browser_family": sample.requested_family,
        "effective_browser_family": sample.effective_family,
        "failure_stage": sample.failure_stage,
    }


def close_browser_acquisition_sample(token: contextvars.Token | None) -> None:
    """Close and reset the scope. Owner-only (a ``None`` token is a no-op). Always resets the
    ContextVar so a sample can never leak into the next acquisition; marks the sample closed first
    so a child task that outlived the acquisition cannot mutate it afterward."""
    if token is None:
        return
    sample = _current_sample.get()
    if sample is not None:
        sample.closed = True
    _current_sample.reset(token)


@contextmanager
def browser_acquisition_scope(acquire_mode: str) -> Iterator[None]:
    """Own a first-try acquisition sample for one create/attach acquisition attempt.

    Opened around the whole acquisition boundary (including the engine boot-fallback retry) and
    entered OUTSIDE the canonical failure-emit context manager, so the sample is still open when
    the canonical ``acquire_result`` event is emitted on both the success and failure paths.

    The scope opens for any attempt except an explicit ``reuse`` (which never reaches this
    boundary): the create-vs-attach decision is not final here, because dynamic routing can select
    a vendor branch that provisions a new session and ignores a fallback ``browser_address``. So
    every attempt is sampled and inclusion is decided at emit time by the resolved ``acquire_mode``
    — a true attach terminates with ``acquire_mode="attach"`` and its fields are dropped there,
    never entering the session-creation denominator."""
    token = begin_browser_acquisition_sample() if acquire_mode != "reuse" else None
    try:
        yield
    finally:
        close_browser_acquisition_sample(token)
