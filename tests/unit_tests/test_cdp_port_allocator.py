import socket

import pytest

from skyvern.webeye.cdp_ports import (
    _CDP_PORT_RANGE_END,
    _CDP_PORT_RANGE_START,
    _allocate_cdp_port,
    _allocated_ports,
    _release_cdp_port,
)


@pytest.fixture(autouse=True)
def _clean_allocated_ports():
    """Ensure the global allocated-ports set is empty before and after each test."""
    _allocated_ports.clear()
    yield
    _allocated_ports.clear()


class TestAllocateCdpPort:
    def test_returns_port_in_range(self):
        port = _allocate_cdp_port()
        assert _CDP_PORT_RANGE_START <= port <= _CDP_PORT_RANGE_END

    def test_port_is_tracked(self):
        port = _allocate_cdp_port()
        assert port in _allocated_ports

    def test_consecutive_calls_return_different_ports(self):
        p1 = _allocate_cdp_port()
        p2 = _allocate_cdp_port()
        assert p1 != p2

    def test_skips_already_allocated_ports(self):
        first = _allocate_cdp_port()
        second = _allocate_cdp_port()
        assert second != first
        assert {first, second}.issubset(_allocated_ports)

    def test_skips_ports_bound_by_other_processes(self):
        # Bind the first port in the range so the allocator must skip it.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", _CDP_PORT_RANGE_START))
        try:
            port = _allocate_cdp_port()
            assert port != _CDP_PORT_RANGE_START
            assert port in _allocated_ports
        finally:
            sock.close()

    def test_raises_when_range_exhausted(self):
        # Mark every port in the range as allocated.
        for p in range(_CDP_PORT_RANGE_START, _CDP_PORT_RANGE_END + 1):
            _allocated_ports.add(p)

        with pytest.raises(RuntimeError, match="No available CDP ports"):
            _allocate_cdp_port()


class TestReleaseCdpPort:
    def test_release_removes_from_tracking(self):
        port = _allocate_cdp_port()
        _release_cdp_port(port)
        assert port not in _allocated_ports

    def test_release_allows_reallocation(self):
        p1 = _allocate_cdp_port()
        _release_cdp_port(p1)
        p2 = _allocate_cdp_port()
        assert p2 == p1

    def test_release_nonexistent_port_is_noop(self):
        _release_cdp_port(99999)
        assert 99999 not in _allocated_ports
