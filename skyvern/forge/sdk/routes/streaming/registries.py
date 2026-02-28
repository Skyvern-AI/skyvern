"""
Contains registries for coordinating active WS connections (aka "channels", see
`./channels/README.md`).

NOTE: in AWS we had to turn on what amounts to sticky sessions for frontend apps,
so that an individual frontend app instance is guaranteed to always connect to
the same backend api instance. This is beccause the two registries here are
tied together via a `client_id` string.

The tale-of-the-tape is this:
  - frontend app requires two different channels (WS connections) to the backend api
    - one dedicated to streaming VNC's RFB protocol
    - the other dedicated to messaging (JSON)
  - both of these channels are stateful and need to coordinate with one another
"""

from __future__ import annotations

import typing as t

import structlog

if t.TYPE_CHECKING:
    from skyvern.forge.sdk.routes.streaming.cdp_input import CdpInputChannel
    from skyvern.forge.sdk.routes.streaming.channels.message import MessageChannel
    from skyvern.forge.sdk.routes.streaming.channels.vnc import VncChannel

LOG = structlog.get_logger()


# a registry for VNC channels, keyed by `client_id`
vnc_channels: dict[str, VncChannel] = {}


def add_vnc_channel(vnc_channel: VncChannel) -> None:
    vnc_channels[vnc_channel.client_id] = vnc_channel


def get_vnc_channel(client_id: str) -> t.Union[VncChannel, None]:
    return vnc_channels.get(client_id, None)


def del_vnc_channel(client_id: str) -> None:
    try:
        del vnc_channels[client_id]
    except KeyError:
        pass


# a registry for message channels, keyed by `client_id`
message_channels: dict[str, MessageChannel] = {}


def add_message_channel(message_channel: MessageChannel) -> None:
    message_channels[message_channel.client_id] = message_channel


def get_message_channel(client_id: str) -> t.Union[MessageChannel, None]:
    candidate = message_channels.get(client_id, None)

    if candidate and candidate.is_open:
        return candidate

    if candidate:
        LOG.info(
            "MessageChannel: message channel is not open; deleting it",
            client_id=candidate.client_id,
        )

        del_message_channel(candidate.client_id)

    return None


def del_message_channel(client_id: str) -> None:
    try:
        del message_channels[client_id]
    except KeyError:
        pass


# Stream reference counts per workflow_run_id.
# When count > 0, browser state should not be evicted from pages dict.
_stream_refcounts: dict[str, int] = {}


def stream_ref_inc(workflow_run_id: str) -> None:
    _stream_refcounts[workflow_run_id] = _stream_refcounts.get(workflow_run_id, 0) + 1


def stream_ref_dec(workflow_run_id: str) -> None:
    count = _stream_refcounts.get(workflow_run_id, 0) - 1
    if count <= 0:
        _stream_refcounts.pop(workflow_run_id, None)
        # Evict kept browser state now that no streams reference it
        from skyvern.forge import app

        app.BROWSER_MANAGER.pages.pop(workflow_run_id, None)
    else:
        _stream_refcounts[workflow_run_id] = count


def stream_ref_active(workflow_run_id: str) -> bool:
    return _stream_refcounts.get(workflow_run_id, 0) > 0


# a registry for CDP input channels, keyed by `client_id`
cdp_input_channels: dict[str, CdpInputChannel] = {}


def add_cdp_input_channel(channel: CdpInputChannel) -> None:
    cdp_input_channels[channel.client_id] = channel


def get_cdp_input_channel(client_id: str) -> t.Union[CdpInputChannel, None]:
    return cdp_input_channels.get(client_id, None)


def del_cdp_input_channel(client_id: str) -> None:
    try:
        del cdp_input_channels[client_id]
    except KeyError:
        pass
