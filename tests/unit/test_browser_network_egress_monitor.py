import gc
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from weakref import ref

import pytest

from skyvern.forge.sdk.browser_action_policy import AuthorityState, RuntimeOriginAuthority, canonicalize_origin
from skyvern.forge.sdk.browser_network_egress_monitor import (
    BrowserNetworkDenialReason,
    BrowserNetworkEgressMonitor,
)
from tests.unit.browser_effect_approval_test_helpers import run_with_consumed_effect


class _Context:
    def __init__(self, pages: list[object] | None = None) -> None:
        self.pages = pages or []
        self.service_workers: list[object] = []
        self._impl_obj = SimpleNamespace(_options={"serviceWorkers": "block"})
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    async def add_init_script(self, _: str) -> None: ...

    async def route(self, _: str, handler: Any) -> None:
        self.route_handler = handler


class _Page:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler


class _EqualPage(_Page):
    def __eq__(self, other: object) -> bool:
        return isinstance(other, _EqualPage)

    def __hash__(self) -> int:
        return 1


def _authority(url: str = "https://trusted.example") -> RuntimeOriginAuthority:
    origin = canonicalize_origin(url)
    assert origin is not None
    return RuntimeOriginAuthority(AuthorityState.ESTABLISHED, frozenset({origin}))


async def _installed(url: str = "https://trusted.example") -> BrowserNetworkEgressMonitor:
    monitor = BrowserNetworkEgressMonitor()
    await monitor.install(_Context())  # type: ignore[arg-type]
    monitor.bind_authority(_authority(url))
    return monitor


@pytest.mark.asyncio
async def test_structural_consumed_approval_cannot_open_an_epoch() -> None:
    monitor = await _installed()
    forged = SimpleNamespace(consumption_id="forged")

    with pytest.raises(TypeError, match="active consumed approval"):
        with monitor.open_causal_epoch(forged):  # type: ignore[arg-type]
            raise AssertionError("a structural stand-in must not open an epoch")

    assert monitor.invalidation_reason is BrowserNetworkDenialReason.MONITOR_INVALIDATED


@pytest.mark.asyncio
async def test_epoch_close_denies_same_origin_passive_request() -> None:
    monitor = await _installed()

    async def exercise(consumed: Any) -> None:
        with monitor.open_causal_epoch(consumed):
            assert monitor.authorize_request(
                method="GET", url="https://trusted.example/a.png", resource_type="image", frame=object()
            )

    await run_with_consumed_effect(exercise)
    assert not monitor.authorize_request(
        method="GET", url="https://trusted.example/a.png", resource_type="image", frame=object()
    )


@pytest.mark.asyncio
async def test_exact_initial_effect_is_atomic_one_shot_and_callback_task_safe() -> None:
    monitor = await _installed()

    async def exercise(consumed: Any) -> None:
        with monitor.open_causal_epoch(consumed):
            monitor.arm_initial_effect(consumed, method="GET", url="https://trusted.example/report.pdf")

            async def request_stage() -> tuple[bool, bool, bool]:
                mismatch = monitor.authorize_request(
                    method="GET", url="https://trusted.example/raced", resource_type="document", frame=object()
                )
                assert not monitor.authorize_request(
                    method="GET", url="https://trusted.example/report.pdf", resource_type="fetch", frame=object()
                )
                exact = monitor.authorize_request(
                    method="GET", url="https://trusted.example/report.pdf", resource_type="document", frame=object()
                )
                replay = monitor.authorize_request(
                    method="GET", url="https://trusted.example/report.pdf", resource_type="document", frame=object()
                )
                return mismatch, exact, replay

            assert await request_stage() == (False, True, False)
            assert not monitor.authorize_request(
                method="GET", url="https://trusted.example/hop-2", resource_type="document", frame=object()
            )
            assert not monitor.authorize_request(
                method="GET", url="https://trusted.example/hop-3", resource_type="document", frame=object()
            )

    await run_with_consumed_effect(exercise)


@pytest.mark.asyncio
async def test_initial_effect_uses_shared_browser_wire_canonical_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = await _installed()

    async def exercise(consumed: Any) -> None:
        with monitor.open_causal_epoch(consumed):
            monitor.arm_initial_effect(
                consumed,
                method="get",
                url="https://TRUSTED.example:443/caf%C3%A9",
            )
            assert monitor.authorize_request(
                method="GET",
                url="https://trusted.example/caf%C3%A9",
                resource_type="document",
                frame=object(),
            )

    await run_with_consumed_effect(exercise)

    second = await _installed()
    logger = Mock()
    monkeypatch.setattr("skyvern.forge.sdk.browser_network_egress_monitor.LOG", logger)
    for unsupported_url in (
        "https://trusted.example/café",
        "https://trusted.example./page",
        "https://trusted.example../page",
    ):
        assert not second.authorize_request(
            method="GET",
            url=unsupported_url,
            resource_type="document",
            frame=object(),
        )
        assert logger.warning.call_args.kwargs["reason"] is BrowserNetworkDenialReason.CANONICAL_TARGET_UNSUPPORTED


@pytest.mark.asyncio
async def test_initial_effect_does_not_alias_websocket_and_http_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = await _installed("http://trusted.example")
    logger = Mock()
    monkeypatch.setattr("skyvern.forge.sdk.browser_network_egress_monitor.LOG", logger)

    async def exercise(consumed: Any) -> None:
        with monitor.open_causal_epoch(consumed):
            monitor.arm_initial_effect(consumed, method="GET", url="ws://trusted.example/socket")
            assert not monitor.authorize_request(
                method="GET",
                url="http://trusted.example/socket",
                resource_type="document",
                frame=object(),
            )

    await run_with_consumed_effect(exercise)
    assert logger.warning.call_args.kwargs["reason"] is BrowserNetworkDenialReason.FRESH_APPROVAL_REQUIRED


@pytest.mark.asyncio
async def test_initial_effect_preserves_custom_method_case(monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = await _installed()
    logger = Mock()
    monkeypatch.setattr("skyvern.forge.sdk.browser_network_egress_monitor.LOG", logger)

    async def exercise(consumed: Any) -> None:
        with monitor.open_causal_epoch(consumed):
            monitor.arm_initial_effect(consumed, method="X-Custom", url="https://trusted.example/effect")
            assert not monitor.authorize_request(
                method="x-custom",
                url="https://trusted.example/effect",
                resource_type="document",
                frame=object(),
            )

    await run_with_consumed_effect(exercise)
    assert logger.warning.call_args.kwargs["reason"] is BrowserNetworkDenialReason.FRESH_APPROVAL_REQUIRED


@pytest.mark.asyncio
async def test_internal_route_denies_same_origin_redirect_inside_epoch() -> None:
    monitor = await _installed()
    route = SimpleNamespace(
        fetch=AsyncMock(return_value=SimpleNamespace(status=302, headers={"Location": "/next"})),
        abort=AsyncMock(),
        fulfill=AsyncMock(),
    )
    request = SimpleNamespace(method="GET", url="https://trusted.example/a.png", resource_type="image", frame=object())

    async def exercise(consumed: Any) -> None:
        with monitor.open_causal_epoch(consumed):
            await monitor.handle_route(route, request)

    await run_with_consumed_effect(exercise)
    route.abort.assert_awaited_once_with("blockedbyclient")
    route.fulfill.assert_not_awaited()


@pytest.mark.asyncio
async def test_internal_route_consumes_exact_initial_effect_once() -> None:
    monitor = await _installed()
    request = SimpleNamespace(
        method="GET", url="https://trusted.example/page", resource_type="document", frame=object()
    )

    async def exercise(consumed: Any) -> None:
        with monitor.open_causal_epoch(consumed):
            monitor.arm_initial_effect(consumed, method=request.method, url=request.url)
            first = SimpleNamespace(
                fetch=AsyncMock(return_value=SimpleNamespace(status=200, headers={})),
                abort=AsyncMock(),
                fulfill=AsyncMock(),
            )
            await monitor.handle_route(first, request)
            first.fulfill.assert_awaited_once()

            replay = SimpleNamespace(fetch=AsyncMock(), abort=AsyncMock(), fulfill=AsyncMock())
            await monitor.handle_route(replay, request)
            replay.abort.assert_awaited_once_with("blockedbyclient")
            replay.fetch.assert_not_awaited()

    await run_with_consumed_effect(exercise)


@pytest.mark.asyncio
async def test_route_passes_active_request_only_while_cdp_capability_is_registered() -> None:
    monitor = await _installed()
    page = _Page()
    owner = object()
    frame = SimpleNamespace(page=page)
    request = SimpleNamespace(method="GET", url="https://trusted.example/page", resource_type="document", frame=frame)
    monitor.register_active_request_interceptor(page=page, owner=owner)

    async def cdp_authorized_fetch(**_: object) -> SimpleNamespace:
        assert monitor.authorize_request(
            method=request.method,
            url=request.url,
            resource_type=request.resource_type,
            frame=request.frame,
        )
        return SimpleNamespace(status=200, headers={})

    covered = SimpleNamespace(
        continue_=AsyncMock(),
        fetch=AsyncMock(side_effect=cdp_authorized_fetch),
        abort=AsyncMock(),
        fulfill=AsyncMock(),
    )

    async def exercise(consumed: Any) -> None:
        with monitor.open_causal_epoch(consumed):
            monitor.arm_initial_effect(consumed, method=request.method, url=request.url)
            await monitor.handle_route(covered, request)

    await run_with_consumed_effect(exercise)
    covered.fetch.assert_awaited_once_with(max_redirects=0)
    covered.continue_.assert_not_awaited()
    assert covered.fulfill.call_args.kwargs["headers"]["Content-Security-Policy"] == "connect-src 'none'"

    monitor.unregister_active_request_interceptor(page=page, owner=owner)
    uncovered = SimpleNamespace(continue_=AsyncMock(), fetch=AsyncMock(), abort=AsyncMock())
    await monitor.handle_route(uncovered, request)
    uncovered.abort.assert_awaited_once_with("blockedbyclient")
    uncovered.continue_.assert_not_awaited()
    uncovered.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_interceptor_registration_is_weak_and_strictly_identity_keyed() -> None:
    monitor = await _installed()
    registered_page = _EqualPage()
    equal_page = _EqualPage()
    assert registered_page == equal_page and registered_page is not equal_page
    owner = object()
    monitor.register_active_request_interceptor(page=registered_page, owner=owner)

    route = SimpleNamespace(continue_=AsyncMock(), fetch=AsyncMock(), abort=AsyncMock())
    request = SimpleNamespace(
        method="GET",
        url="https://trusted.example/page",
        resource_type="document",
        frame=SimpleNamespace(page=equal_page),
    )
    await monitor.handle_route(route, request)
    route.abort.assert_awaited_once_with("blockedbyclient")
    route.continue_.assert_not_awaited()
    route.fetch.assert_not_awaited()

    registered_ref = ref(registered_page)
    del registered_page
    gc.collect()
    assert registered_ref() is None
    assert monitor._active_request_interceptors == {}


@pytest.mark.asyncio
async def test_navigation_aborts_prior_passive_response_but_keeps_current_dispatch_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = await _installed()
    logger = Mock()
    monkeypatch.setattr("skyvern.forge.sdk.browser_network_egress_monitor.LOG", logger)

    async def navigate_during_fetch(**_: object) -> SimpleNamespace:
        monitor._on_frame_navigated(SimpleNamespace(parent_frame=None))
        return SimpleNamespace(status=200, headers={})

    route = SimpleNamespace(fetch=AsyncMock(side_effect=navigate_during_fetch), abort=AsyncMock(), fulfill=AsyncMock())
    request = SimpleNamespace(
        method="GET", url="https://trusted.example/old.png", resource_type="image", frame=object()
    )

    async def exercise(consumed: Any) -> None:
        with monitor.open_causal_epoch(consumed):
            await monitor.handle_route(route, request)
            assert monitor.authorize_request(
                method="GET", url="https://trusted.example/new.png", resource_type="image", frame=object()
            )

    await run_with_consumed_effect(exercise)
    route.abort.assert_awaited_once_with("blockedbyclient")
    route.fulfill.assert_not_awaited()
    assert logger.warning.call_args_list[0].kwargs["reason"] is BrowserNetworkDenialReason.CAUSAL_EPOCH_REQUIRED


@pytest.mark.asyncio
async def test_preexisting_context_is_rejected_and_unenrolled_is_deny_all() -> None:
    monitor = BrowserNetworkEgressMonitor()
    with pytest.raises(RuntimeError, match="without existing pages"):
        await monitor.install(_Context([object()]))  # type: ignore[arg-type]

    assert not BrowserNetworkEgressMonitor.unenrolled().authorize_request(
        method="GET", url="https://trusted.example/a.png", resource_type="image", frame=object()
    )


@pytest.mark.asyncio
async def test_page_created_and_closed_during_install_still_invalidates_enrollment() -> None:
    class RacingContext(_Context):
        async def add_init_script(self, _: str) -> None:
            page = SimpleNamespace(on=lambda *_: None)
            self.pages.append(page)
            self.handlers["page"](page)
            self.pages.clear()

    with pytest.raises(RuntimeError, match="lost the pre-page installation race"):
        await BrowserNetworkEgressMonitor().install(RacingContext())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_authority_loss_and_context_teardown_permanently_invalidate() -> None:
    monitor = await _installed()
    monitor.bind_authority(RuntimeOriginAuthority(AuthorityState.MISSING))
    assert monitor.invalidation_reason is BrowserNetworkDenialReason.MONITOR_INVALIDATED

    second = BrowserNetworkEgressMonitor()
    context = _Context()
    await second.install(context)  # type: ignore[arg-type]
    context.handlers["close"]()
    assert second.invalidation_reason is BrowserNetworkDenialReason.MONITOR_INVALIDATED


@pytest.mark.asyncio
async def test_active_and_passive_missing_scopes_have_distinct_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = await _installed()
    logger = Mock()
    monkeypatch.setattr("skyvern.forge.sdk.browser_network_egress_monitor.LOG", logger)

    assert not monitor.authorize_request(
        method="GET", url="https://trusted.example/a.png", resource_type="image", frame=object()
    )
    assert not monitor.authorize_request(
        method="GET", url="https://trusted.example/page", resource_type="document", frame=object()
    )
    assert [call.kwargs["reason"] for call in logger.warning.call_args_list] == [
        BrowserNetworkDenialReason.CAUSAL_EPOCH_REQUIRED,
        BrowserNetworkDenialReason.FRESH_APPROVAL_REQUIRED,
    ]


@pytest.mark.asyncio
async def test_consumed_approval_replay_and_capacity_invalidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = await _installed()

    async def replay(consumed: Any) -> None:
        with monitor.open_causal_epoch(consumed):
            pass
        with pytest.raises(RuntimeError, match="already opened"):
            with monitor.open_causal_epoch(consumed):
                pass

    await run_with_consumed_effect(replay)
    assert monitor.invalidation_reason is BrowserNetworkDenialReason.APPROVAL_REPLAYED

    second = await _installed()
    monkeypatch.setattr("skyvern.forge.sdk.browser_network_egress_monitor._MAX_CONSUMED_APPROVALS", 1)

    async def open_once(consumed: Any) -> None:
        with second.open_causal_epoch(consumed):
            pass

    await run_with_consumed_effect(open_once)
    with pytest.raises(RuntimeError, match="capacity"):
        await run_with_consumed_effect(open_once)
    assert second.invalidation_reason is BrowserNetworkDenialReason.APPROVAL_CAPACITY_EXHAUSTED


@pytest.mark.asyncio
async def test_observing_monitor_registers_interception_and_never_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """SKY-13810: an observing monitor is enrolled by run-scoped authority alone, so
    request-interceptor registration succeeds without the monitor owning context routing —
    and it can never deny, whatever the authorization verdict would have been."""
    monitor = BrowserNetworkEgressMonitor.observing("https://run.example/start")
    logger = Mock()
    monkeypatch.setattr("skyvern.forge.sdk.browser_network_egress_monitor.LOG", logger)

    monitor.register_active_request_interceptor(page=(page := _Page()), owner=(owner := object()))

    # Every verdict that would fail closed under enforcement: off-authority origin, passive
    # resource with no causal epoch, and an unarmed document.
    assert monitor.authorize_request(
        method="GET", url="https://cdn.other.example/a.png", resource_type="image", frame=object()
    )
    assert monitor.authorize_request(
        method="GET", url="https://run.example/a.png", resource_type="image", frame=object()
    )
    assert monitor.authorize_request(
        method="POST", url="https://run.example/submit", resource_type="document", frame=object()
    )
    # Reason plus origin, because the deny set cannot be measured from the reason alone; sampled
    # because nothing opens a causal epoch yet, so this is one line per request.
    assert [
        (call.kwargs["reason"], call.kwargs["origin"], call.kwargs["sampling"]) for call in logger.info.call_args_list
    ] == [
        (BrowserNetworkDenialReason.ORIGIN_NOT_AUTHORIZED, "https://cdn.other.example", True),
        (BrowserNetworkDenialReason.CAUSAL_EPOCH_REQUIRED, "https://run.example", True),
        (BrowserNetworkDenialReason.FRESH_APPROVAL_REQUIRED, "https://run.example", True),
    ]
    logger.warning.assert_not_called()

    monitor.unregister_active_request_interceptor(page=page, owner=owner)
    # An invalidated observing monitor still cannot block; nothing degrades into a closed door.
    monitor.invalidate()
    assert monitor.authorize_request(
        method="GET", url="https://run.example/a.png", resource_type="image", frame=object()
    )


@pytest.mark.asyncio
async def test_observing_monitor_refuses_context_routing_and_unusable_run_urls() -> None:
    """Observation and enforcement are exclusive: an observing monitor must never install the
    deny-by-default context route, and a run with no canonical origin yields no authority."""
    monitor = BrowserNetworkEgressMonitor.observing("https://run.example")
    with pytest.raises(RuntimeError, match="never owns context routing"):
        await monitor.install(_Context())  # type: ignore[arg-type]
    # A refused install must not promote it into an enforcing monitor: the authority it mints is
    # a reporting ceiling, never something a later enforcement seam can read as a live grant.
    assert monitor.authorize_request(
        method="GET", url="https://cdn.other.example/a.png", resource_type="image", frame=object()
    )

    for unusable in (None, "", "not a url", "file:///etc/passwd"):
        degraded = BrowserNetworkEgressMonitor.observing(unusable)
        assert degraded.invalidation_reason is BrowserNetworkDenialReason.UNENROLLED
        with pytest.raises(RuntimeError, match="cannot be registered"):
            degraded.register_active_request_interceptor(page=_Page(), owner=object())
