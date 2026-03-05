"""CDP port allocation for local browser sessions."""

from __future__ import annotations

import socket

_CDP_PORT_RANGE_START = 9223
_CDP_PORT_RANGE_END = 9322
_allocated_ports: set[int] = set()


def _allocate_cdp_port() -> int:
    """Find an available port in the CDP port range for a browser session."""
    for port in range(_CDP_PORT_RANGE_START, _CDP_PORT_RANGE_END + 1):
        if port in _allocated_ports:
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                _allocated_ports.add(port)
                return port
        except OSError:
            pass
    raise RuntimeError(f"No available CDP ports in range {_CDP_PORT_RANGE_START}-{_CDP_PORT_RANGE_END}")


def _release_cdp_port(port: int) -> None:
    """Return a CDP port to the available pool."""
    _allocated_ports.discard(port)
