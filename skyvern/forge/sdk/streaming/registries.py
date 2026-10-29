"""
Contains registries for coordinating active WS connections (aka "channels", see
`./channels/README.md`).

NOTE: in AWS we had to turn on what amounts to sticky sessions for frontend apps,
so that an individual frontend app instance is guaranteed to always connect to
the same backend api instance. This is because the two registries here are
tied together via a `client_id` string.

The tale-of-the-tape is this:
  - legacy VNC viewers open two paired channels (WS connections) to the backend api
    - one dedicated to streaming VNC's RFB protocol
    - the other dedicated to messaging (JSON)
  - the CDP path opens only the Message channel here; frames ride CDP
    (screencast) instead of VNC
  - stateful channels sharing a `client_id` need to coordinate with one another

Additionally, this module manages:
  - CDP input channels for interactive browser control
  - Stream reference counts that defer browser state cleanup while active
    CDP streams hold references to a workflow run's browser
"""

from __future__ import annotations

import asyncio
import typing as t
from dataclasses import dataclass

import structlog

if t.TYPE_CHECKING:
    from skyvern.forge.sdk.executor.run_liveness import RunOwner
    from skyvern.forge.sdk.routes.streaming.cdp_input import CdpInputChannel
    from skyvern.forge.sdk.routes.streaming.channels.message import MessageChannel
    from skyvern.forge.sdk.routes.streaming.channels.vnc import VncChannel
    from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()

STREAM_FINALIZER_RELEASE_MAX_ATTEMPTS = 3
STREAM_FINALIZER_RELEASE_RETRY_DELAY_SECONDS = 0.25


# a registry for VNC channels, keyed by `client_id`
vnc_channels: dict[str, VncChannel] = {}


def add_vnc_channel(vnc_channel: VncChannel) -> None:
    vnc_channels[vnc_channel.client_id] = vnc_channel


def get_vnc_channel(client_id: str) -> VncChannel | None:
    return vnc_channels.get(client_id)


def del_vnc_channel(client_id: str, *, expected: VncChannel | None = None) -> None:
    candidate = vnc_channels.get(client_id)

    if candidate is None:
        return

    # Prevent stale channel shutdown from deleting a newer channel that reused
    # the same client_id during route transitions/reconnects.
    if expected is not None and candidate is not expected:
        return

    del vnc_channels[client_id]


# a registry for message channels, keyed by `client_id`
message_channels: dict[str, MessageChannel] = {}


def add_message_channel(message_channel: MessageChannel) -> None:
    message_channels[message_channel.client_id] = message_channel


def get_message_channel(client_id: str) -> MessageChannel | None:
    candidate = message_channels.get(client_id)

    if candidate is None:
        return None

    if candidate.is_open:
        return candidate

    LOG.info(
        "MessageChannel: message channel is not open; deleting it",
        client_id=candidate.client_id,
    )
    del_message_channel(candidate.client_id, expected=candidate)
    return None


def del_message_channel(client_id: str, *, expected: MessageChannel | None = None) -> None:
    candidate = message_channels.get(client_id)

    if candidate is None:
        return

    # Prevent stale channel shutdown from deleting a newer channel that reused
    # the same client_id during route transitions/reconnects.
    if expected is not None and candidate is not expected:
        return

    del message_channels[client_id]


# Stream reference counts per workflow_run_id.
_stream_refcounts: dict[str, int] = {}


@dataclass(frozen=True)
class DeferredCloseParams:
    close_browser_on_completion: bool
    release_driver: bool | None
    browser_session_id: str | None = None
    organization_id: str | None = None
    expected_runnable_id: str | None = None
    expected_runnable_generation_id: str | None = None
    expected_browser_state: BrowserState | None = None
    # The browser the deferring cleanup captured and that cleanup's execution, so a successor's browser is kept.
    deferred_browser_state: BrowserState | None = None
    owner: RunOwner | None = None


@dataclass
class _ClosingStreamState:
    params: DeferredCloseParams | None = None
    drained: bool = False
    finalizer_started: bool = False


_deferred_close_params: dict[str, DeferredCloseParams] = {}
_closing_streams: dict[str, _ClosingStreamState] = {}


def try_stream_ref_inc(workflow_run_id: str) -> bool:
    """Atomically attach unless cleanup already installed its no-await closing tombstone."""
    if workflow_run_id in _closing_streams:
        return False
    _stream_refcounts[workflow_run_id] = _stream_refcounts.get(workflow_run_id, 0) + 1
    return True


def stream_ref_inc(workflow_run_id: str) -> None:
    if not try_stream_ref_inc(workflow_run_id):
        raise RuntimeError(f"Workflow run {workflow_run_id} is closing")


def mark_stream_closing(workflow_run_id: str) -> None:
    """Reject all later attaches. Contains no await, so it serializes with try_stream_ref_inc."""
    _closing_streams.setdefault(
        workflow_run_id,
        _ClosingStreamState(drained=not stream_ref_active(workflow_run_id)),
    )


def claim_stream_closing(workflow_run_id: str) -> _ClosingStreamState | None:
    """``mark_stream_closing`` that also returns the tombstone when this call installed it."""
    if workflow_run_id in _closing_streams:
        return None
    mark_stream_closing(workflow_run_id)
    return _closing_streams[workflow_run_id]


def pending_deferred_close_params(workflow_run_id: str) -> DeferredCloseParams | None:
    state = _closing_streams.get(workflow_run_id)
    return state.params if state is not None else None


def release_stream_closing(workflow_run_id: str, tombstone: _ClosingStreamState) -> None:
    # Only the caller's own tombstone, and only while no deferred close finalizer owns it.
    if _closing_streams.get(workflow_run_id) is tombstone and tombstone.params is None:
        del _closing_streams[workflow_run_id]


def complete_owned_stream_teardown(workflow_run_id: str, tombstone: _ClosingStreamState) -> None:
    """``complete_stream_teardown`` for the caller's own tombstone after its close ran inline: the streams drained,
    so the stored deferral was never armed and no finalizer runs."""
    if (
        _closing_streams.get(workflow_run_id) is tombstone
        and tombstone.drained
        and not tombstone.finalizer_started
        and workflow_run_id not in _deferred_close_params
    ):
        del _closing_streams[workflow_run_id]


def _take_ready_finalizer(workflow_run_id: str) -> DeferredCloseParams | None:
    state = _closing_streams.get(workflow_run_id)
    if state is None or state.params is None or not state.drained or state.finalizer_started:
        return None
    state.finalizer_started = True
    return state.params


async def _run_stream_finalizer(workflow_run_id: str, params: DeferredCloseParams) -> None:
    from skyvern.forge import app
    from skyvern.forge.sdk.executor.run_liveness import is_superseded_owner

    browser_state = app.BROWSER_MANAGER.pages.get(workflow_run_id)
    close_state = params.deferred_browser_state or params.expected_browser_state or browser_state
    # A successor registered since the deferral that still holds this browser adopted it: it is no longer ours to
    # close, unpublish or release. A newer adopter keeps it even after a rebind revoked its authority.
    handed_over = browser_state is close_state and (
        is_superseded_owner(workflow_run_id, params.owner)
        or (
            params.owner is not None
            and close_state is not None
            and app.BROWSER_MANAGER.is_held_by_newer_adopter(workflow_run_id, params.owner, close_state)
        )
    )
    try:
        # Unpublished before the close awaits, so a successor arriving meanwhile gets its own browser; a page some
        # other execution published is left alone.
        if not handed_over and (browser_state is None or browser_state is close_state):
            app.BROWSER_MANAGER.evict_page(workflow_run_id)
        if browser_state is close_state and close_state is not None and not handed_over:
            try:
                await close_state.close(
                    close_browser_on_completion=params.close_browser_on_completion,
                    release_driver=params.release_driver,
                )
            except Exception:
                LOG.warning(
                    "stream_ref_dec: error closing deferred browser state",
                    workflow_run_id=workflow_run_id,
                    exc_info=True,
                )
        elif (
            handed_over
            and params.browser_session_id is not None
            and params.organization_id is not None
            and params.expected_runnable_id is not None
            and params.expected_browser_state is not None
        ):
            # The successor keeps using the session, so it inherits the cleanup lease this deferral held.
            app.BROWSER_MANAGER.inherit_deferred_session_lease(
                workflow_run_id,
                session_id=params.browser_session_id,
                organization_id=params.organization_id,
                runnable_id=params.expected_runnable_id,
                runnable_generation_id=params.expected_runnable_generation_id,
                browser_state=params.expected_browser_state,
            )
        elif params.owner is not None and close_state is not None and browser_state is not close_state:
            # A successor replaced the page, displacing this execution's browser; its own claim settles it once.
            claim = app.BROWSER_MANAGER.claim_superseded_run_resources(workflow_run_id, params.owner)
            await app.BROWSER_MANAGER.release_superseded_run_resources(workflow_run_id, claim)
        if (
            not handed_over
            and params.browser_session_id is not None
            and params.organization_id is not None
            and params.expected_runnable_id is not None
            and params.expected_browser_state is not None
        ):
            release_error: Exception | None = None
            for attempt in range(1, STREAM_FINALIZER_RELEASE_MAX_ATTEMPTS + 1):
                try:
                    released = await app.PERSISTENT_SESSIONS_MANAGER.release_browser_session(
                        session_id=params.browser_session_id,
                        organization_id=params.organization_id,
                        expected_runnable_id=params.expected_runnable_id,
                        expected_runnable_generation_id=params.expected_runnable_generation_id,
                        expected_browser_state=params.expected_browser_state,
                    )
                    if released:
                        release_error = None
                        break
                    release_error = RuntimeError("Deferred persistent-session owner release did not complete")
                except Exception as exc:
                    release_error = exc
                if attempt < STREAM_FINALIZER_RELEASE_MAX_ATTEMPTS:
                    await asyncio.sleep(STREAM_FINALIZER_RELEASE_RETRY_DELAY_SECONDS * attempt)
            if release_error is not None:
                raise release_error
    except Exception:
        # The release already exhausted its retry budget above, and stream_ref_dec has popped the
        # refcount, so nothing can re-enter this finalizer. Drop the tombstone anyway instead of
        # rejecting every later attach; reclaiming the occupied row is the reaper's job.
        LOG.warning(
            "stream_ref_dec: deferred persistent-session release failed; leaving the row to the reaper",
            workflow_run_id=workflow_run_id,
            browser_session_id=params.browser_session_id,
            exc_info=True,
        )

    # Only this finalizer's own deferral is completed; one a successor armed or installed meanwhile stays.
    state = _closing_streams.get(workflow_run_id)
    if state is not None and state.params is params:
        del _closing_streams[workflow_run_id]
    if _deferred_close_params.get(workflow_run_id) is params:
        del _deferred_close_params[workflow_run_id]


async def finalize_stream_teardown(workflow_run_id: str) -> None:
    params = _take_ready_finalizer(workflow_run_id)
    if params is not None:
        await _run_stream_finalizer(workflow_run_id, params)


def complete_stream_teardown(workflow_run_id: str) -> None:
    """Clear the tombstone after owner cleanup completed the inline no-stream path."""
    _closing_streams.pop(workflow_run_id, None)
    _deferred_close_params.pop(workflow_run_id, None)


async def stream_ref_dec(workflow_run_id: str) -> None:
    count = _stream_refcounts.get(workflow_run_id, 0) - 1
    if count <= 0:
        _stream_refcounts.pop(workflow_run_id, None)
        state = _closing_streams.get(workflow_run_id)
        if state is not None:
            state.drained = True
        await finalize_stream_teardown(workflow_run_id)
    else:
        _stream_refcounts[workflow_run_id] = count


def stream_ref_active(workflow_run_id: str) -> bool:
    return _stream_refcounts.get(workflow_run_id, 0) > 0


def stream_tombstone_holds_session_lease(workflow_run_id: str, browser_session_id: str) -> bool:
    """Whether workflow cleanup still owns its persistent-session lease.

    The closing tombstone is installed before terminal status publication and before cleanup can
    populate deferred-close parameters. Therefore its presence alone holds the run's lease until
    ``complete_stream_teardown`` removes it. ``browser_session_id`` remains in the public contract
    because trusted release supplies both owner identities.
    """
    _ = browser_session_id
    return workflow_run_id in _closing_streams


def set_deferred_close_params(
    workflow_run_id: str,
    close_browser_on_completion: bool,
    release_driver: bool | None = None,
    *,
    browser_session_id: str | None = None,
    organization_id: str | None = None,
    expected_runnable_id: str | None = None,
    expected_runnable_generation_id: str | None = None,
    expected_browser_state: BrowserState | None = None,
    deferred_browser_state: BrowserState | None = None,
    owner: RunOwner | None = None,
) -> bool:
    mark_stream_closing(workflow_run_id)
    params = DeferredCloseParams(
        close_browser_on_completion=close_browser_on_completion,
        release_driver=release_driver,
        browser_session_id=browser_session_id,
        organization_id=organization_id,
        expected_runnable_id=expected_runnable_id,
        expected_runnable_generation_id=expected_runnable_generation_id,
        expected_browser_state=expected_browser_state,
        deferred_browser_state=deferred_browser_state,
        owner=owner,
    )
    _closing_streams[workflow_run_id].params = params
    active = stream_ref_active(workflow_run_id)
    if not active:
        _closing_streams[workflow_run_id].drained = True
    if active:
        _deferred_close_params[workflow_run_id] = params
    return active


# a registry for CDP input channels, keyed by `client_id`
cdp_input_channels: dict[str, CdpInputChannel] = {}


def add_cdp_input_channel(channel: CdpInputChannel) -> None:
    cdp_input_channels[channel.client_id] = channel


def del_cdp_input_channel(client_id: str) -> None:
    cdp_input_channels.pop(client_id, None)
