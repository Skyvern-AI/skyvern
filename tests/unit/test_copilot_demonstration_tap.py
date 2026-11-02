from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import pytest
from playwright.async_api import async_playwright
from structlog.testing import capture_logs

from skyvern.forge.sdk.copilot import demonstration_tap as tap_module
from skyvern.forge.sdk.copilot.composition_browser_expressions import scout_accessible_role_name_expression
from skyvern.forge.sdk.copilot.demonstration_tap import PageTap, TapEvent
from tests.unit.copilot_test_helpers import skip_no_browser
from tests.unit.scoped_asyncio import ScopedAsyncio

# Linux chromium-headless-shell ignores printable type-ahead on a closed <select> (no input/change fires);
# full Chromium commits it on Linux and macOS, so tests that type into a select launch this channel.
SELECT_TYPE_AHEAD_CHANNEL = "chromium"


def test_page_tap_queue_stops_accepting_events_at_limit() -> None:
    tap = PageTap(SimpleNamespace(), max_events=1)  # type: ignore[arg-type]
    first = TapEvent(SimpleNamespace(), "tap-1", "main", True, "", {"kind": "click", "ts": 1})
    second = TapEvent(SimpleNamespace(), "tap-1", "main", True, "", {"kind": "click", "ts": 2})

    assert tap._enqueue_event(first) is True
    assert tap._enqueue_event(second) is False

    assert tap.event_limit_reached is True
    assert tap.drain_events() == [first]


@pytest.mark.asyncio
async def test_page_tap_frame_scan_logs_an_error_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    tap = PageTap(SimpleNamespace(), max_events=1)  # type: ignore[arg-type]
    scan = AsyncMock(side_effect=[RuntimeError("scan failed"), None])

    async def sleep(_: float) -> None:
        if scan.await_count == 1:
            tap._stopped = True

    monkeypatch.setattr(tap, "_scan_once", scan)
    monkeypatch.setattr(tap_module, "asyncio", ScopedAsyncio(sleep=sleep))

    with capture_logs() as logs:
        await tap.poll_new_frames()

    assert scan.await_count == 2
    assert any(entry["event"] == "copilot_demonstration_frame_scan_failed" for entry in logs)


async def _wait_for_event(
    tap: PageTap, *, main_frame: bool | None = None, **payload: Any
) -> tuple[TapEvent, list[TapEvent]]:
    seen: list[TapEvent] = []
    for _ in range(300):
        seen.extend(tap.drain_events())
        match = next(
            (
                event
                for event in seen
                if all(event.payload.get(key) == value for key, value in payload.items())
                and (main_frame is None or event.is_main_frame == main_frame)
            ),
            None,
        )
        if match is not None:
            return match, seen
        await asyncio.sleep(0.05)
    raise AssertionError(f"tap event not observed: {[event.payload for event in seen]}")


@skip_no_browser
@pytest.mark.asyncio
async def test_page_tap_captures_trusted_actions_navigation_and_oopif() -> None:
    main_origin = "http://main.test"
    frame_origin = "http://frame.test"
    pages = {
        "/": f"""
            <html><body>
              <input id="autofocus" type="text" autofocus><button id="plain">Press</button>
              <label id="text-label" for="text">Text</label><input id="text" type="text">
              <input id="password" type="password">
              <select id="choice"><option value="a">Alpha</option><option value="b">Beta</option></select><input id="check" type="checkbox">
              <label id="check-label" for="check">Check</label><a id="next" href="/two">Next</a>
              <input id="show-secret" name="login-secret" type="password">
              <button id="show-secret-toggle" onclick="const prior = document.querySelector('#show-secret');
                const replacement = document.createElement('input'); replacement.id = 'shown-password';
                replacement.name = 'visible-value'; replacement.type = 'text'; replacement.value = prior.value;
                prior.replaceWith(replacement)">Show password</button>
              <div id="secret-label-fields">
                <label>Credit or debit card<input id="card" type="text"></label>
                <label>Security code<input id="security-code" type="text"></label>
                <label>API token<input id="api-token" type="text"></label>
                <label>Secret key<input id="secret-key" type="text"></label>
                <label>Auth token<input id="auth-token" type="text"></label>
                <label>Routing number<input id="routing" type="text"></label>
                <label>Account number<input id="account" type="text"></label>
                <label>Social security number<input id="ssn" type="text"></label>
                <label>PIN<input id="pin" type="text"></label>
                <label>Recovery code<input id="recovery" type="text"></label>
                <label>CVV<input id="cvv" type="text"></label>
                <label>CVC<input id="cvc" type="text"></label>
                <label>Card number<input id="card-number" type="text"></label>
                <label>Passcode<input id="passcode" type="text"></label>
                <label>Token<input id="token" type="text"></label>
                <label>Secret<input id="secret" type="text"></label>
                <span>Access key</span><input id="access-key" type="text">
                <label>Refresh token<input id="refresh-token" type="text"></label>
                <label>Card no.<input id="card-no" type="text"></label>
                <label>CVV2<input id="cvv2" type="text"></label>
                <label>Bank account<input id="bank-account" type="text"></label>
                <label>Acct #<input id="acct" type="text"></label>
                <label>Account #<input id="account-hash" type="text"></label>
                <label>Social Security #<input id="social-security" type="text"></label>
                <label>Passport<input id="passport" type="text"></label>
                <label>Tax ID<input id="tax-id" type="text"></label>
                <label>Login code<input id="login-code" type="text"></label>
                <label>Access code<input id="access-code" type="text"></label>
                <label>Security answer<input id="security-answer" type="text"></label>
                <label>Seed phrase<input id="seed-phrase" type="text"></label>
                <label>Recovery phrase<input id="recovery-phrase" type="text"></label>
                <label>IBAN<input id="iban" type="text"></label>
                <label>API key<input id="focus-only-secret" type="text" tabindex="-1"></label>
              </div>
              <iframe id="child" name="child-frame" src="{frame_origin}/frame"></iframe>
              <iframe id="writer" name="writer-frame" src="/writer"></iframe>
              <button id="add-frame" onclick="const frame = document.createElement('iframe'); frame.id = 'late';
                frame.name = 'late-frame'; document.body.append(frame)">Add frame</button>
              <div id="shadow-host"></div>
              <script>
                const root = document.querySelector('#shadow-host').attachShadow({{mode: 'open'}});
                root.innerHTML = '<button id="shadow-button">Shadow button</button>'
                  + '<select id="shadow-select"><option value="a">A</option><option value="b">B</option></select>';
              </script>
            </body></html>
        """.encode(),
        "/frame": f'<html><body><button id="frame-button">Frame button</button><iframe id="nested" src="{frame_origin}/nested"></iframe></body></html>'.encode(),
        "/nested": b'<html><body><button id="nested-button">Nested button</button></body></html>',
        "/writer": b'<html><body><button id="before-write">Before</button></body></html>',
        "/late": b'<html><body><button id="late-button">Late frame</button></body></html>',
        "/two": b'<html><body><button id="after">After navigation</button><input id="final"><input id="slider" type="range" min="0" max="10"></body></html>',
        "/submit-form": b'<html><body><form action="/submitted"><input id="submit-value"><button type="submit">Submit</button></form></body></html>',
        "/submitted": b"<html><body>Submitted</body></html>",
    }
    tap: PageTap | None = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                channel=SELECT_TYPE_AHEAD_CHANNEL,
                args=["--site-per-process", "--use-mock-keychain", "--password-store=basic"],
            )
            context = await browser.new_context()

            async def serve(route: Any) -> None:
                body = pages.get(urlparse(route.request.url).path, b"not found")
                await route.fulfill(
                    status=200 if body != b"not found" else 404,
                    content_type="text/html",
                    body=body,
                )

            await context.route("**/*", serve)
            page = await context.new_page()
            await page.goto(f"{main_origin}/two")
            await page.goto(f"{main_origin}/")
            tap = PageTap(context)
            await tap.start([page])

            assert await page.evaluate("typeof globalThis.__skyvernDemoTap") == "undefined"

            await page.go_back()
            await page.go_forward()
            await page.wait_for_timeout(200)
            assert not any(event.payload.get("kind") == "navigate" for event in tap.drain_events())

            await page.goto(f"{main_origin}/two")
            navigation, _ = await _wait_for_event(tap, kind="navigate")
            assert navigation.payload["url"] == f"{main_origin}/two"
            assert navigation.payload["transition_type"] == "typed"
            await page.go_back()
            await page.wait_for_timeout(200)
            assert not any(event.payload.get("kind") == "navigate" for event in tap.drain_events())

            tap.begin_control_navigation()
            try:
                await page.goto(f"{main_origin}/two")
            finally:
                tap.end_control_navigation()
            await page.wait_for_timeout(200)
            assert not any(event.payload.get("kind") == "navigate" for event in tap.drain_events())
            await page.go_back()

            await page.locator("#autofocus").focus()
            await page.keyboard.type("first")
            await page.keyboard.press("Enter")
            _, autofocus_events = await _wait_for_event(tap, kind="key", key="Enter")
            assert [(event.payload["kind"], event.payload.get("value")) for event in autofocus_events] == [
                ("type", "first"),
                ("key", None),
            ]
            await page.keyboard.type("second")
            await page.keyboard.press("Enter")
            _, restarted_events = await _wait_for_event(tap, kind="key", key="Enter")
            assert [event.payload["kind"] for event in restarted_events] == ["type", "key"]
            assert restarted_events[0].payload["value"] == "firstsecond"

            await page.click("#plain")
            click, _ = await _wait_for_event(tap, kind="click")
            assert click.payload["selector"] == "#plain"
            assert click.payload["packet"]["selector_match_count"] == 1

            await page.click("#text-label")
            await page.keyboard.type("full typed value")
            await page.keyboard.press("Tab")
            typed, typed_events = await _wait_for_event(tap, kind="type")
            if not any(event.payload.get("key") == "Tab" for event in typed_events):
                _, tab_events = await _wait_for_event(tap, kind="key", key="Tab")
                typed_events.extend(tab_events)
            assert typed.payload["value"] == "full typed value"
            assert not any(event.payload.get("kind") == "click" for event in typed_events)
            assert any(
                event.payload.get("kind") == "key" and event.payload.get("key") == "Tab" for event in typed_events
            )

            secret_focus = next((event for event in typed_events if event.payload.get("kind") == "secret_focus"), None)
            if secret_focus is None:
                secret_focus, _ = await _wait_for_event(tap, kind="secret_focus")
            assert secret_focus.payload["selector"] == "#password"
            await page.keyboard.type("do-not-expose")
            await page.keyboard.press("Tab")
            secret, _ = await _wait_for_event(tap, kind="secret", selector="#password")
            assert secret.payload["typed_length"] == len("do-not-expose")

            await page.click("#check-label")
            _, checkbox_events = await _wait_for_event(tap, kind="click", selector="#check-label")
            await page.wait_for_timeout(1100)
            checkbox_events.extend(tap.drain_events())
            assert "do-not-expose" not in repr([event.payload for event in checkbox_events])
            assert not any(event.payload.get("kind") == "secret" for event in checkbox_events)
            assert [
                event.payload.get("selector") for event in checkbox_events if event.payload.get("kind") == "click"
            ] == ["#check-label"]

            tap.drain_events()
            await page.locator("#show-secret").focus()
            await page.keyboard.type("first-secret")
            await page.click("#show-secret-toggle")
            await page.locator("#shown-password").focus()
            await page.keyboard.type("-still-secret")
            await page.keyboard.press("Tab")
            _, toggled_events = await _wait_for_event(tap, kind="secret", selector="#shown-password")
            await page.wait_for_timeout(100)
            toggled_events.extend(tap.drain_events())
            assert "first-secret" not in repr([event.payload for event in toggled_events])
            assert "still-secret" not in repr([event.payload for event in toggled_events])
            assert not any(
                event.payload.get("kind") == "type"
                and event.payload.get("selector") in {"#show-secret", "#shown-password"}
                for event in toggled_events
            )

            secret_selectors = [
                "#card",
                "#security-code",
                "#api-token",
                "#secret-key",
                "#auth-token",
                "#routing",
                "#account",
                "#ssn",
                "#pin",
                "#recovery",
                "#cvv",
                "#cvc",
                "#card-number",
                "#passcode",
                "#token",
                "#secret",
                "#access-key",
                "#refresh-token",
                "#card-no",
                "#cvv2",
                "#bank-account",
                "#acct",
                "#account-hash",
                "#social-security",
                "#passport",
                "#tax-id",
                "#login-code",
                "#access-code",
                "#security-answer",
                "#seed-phrase",
                "#recovery-phrase",
                "#iban",
            ]
            tap.drain_events()
            for selector in secret_selectors:
                await page.locator(selector).focus()
                await page.keyboard.type("hidden-value")
                await page.keyboard.press("Tab")
            _, labelled_secret_events = await _wait_for_event(tap, kind="secret", selector="#passcode")
            await page.wait_for_timeout(100)
            labelled_secret_events.extend(tap.drain_events())
            assert "hidden-value" not in repr([event.payload for event in labelled_secret_events])
            assert set(secret_selectors) <= {
                event.payload.get("selector")
                for event in labelled_secret_events
                if event.payload.get("kind") == "secret"
            }

            tap.drain_events()
            await page.click("#plain")
            await page.click("#focus-only-secret")
            await page.keyboard.press("Tab")
            await tap.flush()
            focus_only_events = tap.drain_events()
            assert any(event.payload.get("kind") == "secret_focus" for event in focus_only_events)
            assert not any(event.payload.get("kind") == "secret" for event in focus_only_events)

            tap.drain_events()
            await page.evaluate(
                """() => {
                    document.querySelector('#plain').dispatchEvent(new MouseEvent('click', {bubbles: true}));
                    document.querySelector('#choice').dispatchEvent(new Event('change', {bubbles: true}));
                    document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
                }"""
            )
            await page.wait_for_timeout(1100)
            assert tap.drain_events() == []

            await page.locator("#shadow-host").locator("#shadow-button").click()
            shadow_click, _ = await _wait_for_event(tap, selector="#shadow-host #shadow-button")
            assert shadow_click.payload["packet"]["selector_match_count"] == 1

            await page.locator("#shadow-host").locator("#shadow-select").focus()
            await page.keyboard.press("b")
            shadow_select, select_events = await _wait_for_event(tap, kind="select")
            await page.wait_for_timeout(1100)
            select_events.extend(tap.drain_events())
            assert shadow_select.payload["value"] == "b"
            assert sum(event.payload.get("kind") == "select" for event in select_events) == 1

            await page.evaluate("document.activeElement.blur()")
            await page.keyboard.press("Escape")
            escape, _ = await _wait_for_event(tap, kind="key", key="Escape")
            assert "selector" not in escape.payload
            assert "packet" not in escape.payload

            frame = next(frame for frame in page.frames if frame.name == "child-frame")
            await frame.click("#frame-button")
            frame_click, _ = await _wait_for_event(tap, main_frame=False, kind="click")
            assert await tap.frame_owner_selector(frame_click) == "#child"
            nested = next(frame for frame in page.frames if frame.url.endswith("/nested"))
            await nested.click("#nested-button")
            nested_click, _ = await _wait_for_event(tap, main_frame=False, selector="#nested-button")
            assert await tap.frame_path(nested_click) == [{"name": "child-frame"}, {"owner_selector": "#nested"}]

            writer = next(frame for frame in page.frames if frame.name == "writer-frame")
            await writer.evaluate(
                """() => {
                    document.open();
                    document.write('<button id="after-write">After</button>');
                    document.close();
                }"""
            )
            await page.wait_for_timeout(1200)
            await writer.click("#after-write")
            await _wait_for_event(tap, selector="#after-write")

            await page.click("#add-frame")
            late = next(frame for frame in page.frames if frame.name == "late-frame")
            for _ in range(40):
                if any(frame is late and url == "about:blank" for frame, url in tap._refused_frames):
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("about:blank frame was not observed as in-process")
            await page.evaluate(f"document.querySelector('#late').src = '{frame_origin}/late'")
            await late.wait_for_url("**/late")
            await page.wait_for_timeout(700)
            await late.click("#late-button")
            await _wait_for_event(tap, main_frame=False, selector="#late-button")

            await page.locator("#next").evaluate(
                "element => element.addEventListener('auxclick', event => event.preventDefault(), {once: true})"
            )
            tap.drain_events()
            await page.locator("#next").click(button="middle")
            await page.locator("#next").click(button="right")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(700)
            assert not any(
                event.payload.get("kind") == "click" and event.payload.get("selector") == "#next"
                for event in tap.drain_events()
            )

            await page.click("#next")
            await page.wait_for_url("**/two")
            navigating_click, link_events = await _wait_for_event(tap, kind="click", selector="#next")
            await page.wait_for_timeout(200)
            link_events.extend(tap.drain_events())
            assert navigating_click.payload["url"].endswith("/")
            assert (
                sum(
                    event.payload.get("kind") == "click" and event.payload.get("selector") == "#next"
                    for event in link_events
                )
                == 1
            )
            assert not any(event.payload.get("kind") == "navigate" for event in link_events)
            assert await page.evaluate("typeof globalThis.__skyvernDemoTap") == "undefined"

            await page.click("#after")
            await _wait_for_event(tap, kind="click", selector="#after")
            tap.drain_events()
            await page.click("#slider")
            await page.keyboard.press("ArrowRight")
            await page.keyboard.press("Tab")
            slider, slider_events = await _wait_for_event(tap, kind="unsupported_input", selector="#slider")
            assert int(slider.payload["value"]) > 5
            assert not any(
                event.payload.get("kind") == "click" and event.payload.get("selector") == "#slider"
                for event in slider_events
            )

            tap.begin_control_navigation()
            try:
                await page.goto(f"{main_origin}/submit-form")
            finally:
                tap.end_control_navigation()
            await page.wait_for_timeout(200)
            tap.drain_events()
            await page.click("#submit-value")
            await page.keyboard.type("final answer")
            await page.keyboard.press("Enter")
            await page.wait_for_url("**/submitted*")
            _, submit_events = await _wait_for_event(tap, kind="key", key="Enter")
            assert [event.payload.get("kind") for event in submit_events] == ["type", "key"]
            assert submit_events[0].payload["value"] == "final answer"

            tap.begin_control_navigation()
            try:
                await page.goto(f"{main_origin}/two")
            finally:
                tap.end_control_navigation()
            tap.drain_events()
            await page.click("#final")
            await page.keyboard.type("last field value")
            await tap.stop()
            final_events = tap.drain_events()
            assert [event.payload.get("value") for event in final_events if event.payload.get("kind") == "type"] == [
                "last field value"
            ]
            await page.click("#after")
            await page.wait_for_timeout(1200)
            assert tap.drain_events() == []
            await tap.stop()

            tap = PageTap(context)
            await tap.start([page])
            await page.click("#after")
            await _wait_for_event(tap, kind="click", selector="#after")
            await browser.close()
    finally:
        if tap is not None:
            await tap.stop()


@skip_no_browser
@pytest.mark.asyncio
async def test_page_tap_captures_every_typed_navigation_when_the_renderer_commits_slowly() -> None:
    paths = ["/a", "/b", "/c"]
    tap: PageTap | None = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()

            async def serve(route: Any) -> None:
                await route.fulfill(status=200, content_type="text/html", body=b"<html><body>Page</body></html>")

            await context.route("**/*", serve)
            page = await context.new_page()
            await page.goto("http://main.test/")
            tap = PageTap(context)
            await tap.start([page])
            # A throttled renderer reports frameNavigated well before the browser records the new history entry.
            throttle = await context.new_cdp_session(page)
            await throttle.send("Emulation.setCPUThrottlingRate", {"rate": 100})

            navigated: list[str] = []
            for path in paths:
                await page.goto(f"http://main.test{path}")
                navigation, _ = await _wait_for_event(tap, kind="navigate")
                navigated.append(navigation.payload["url"])
            assert navigated == [f"http://main.test{path}" for path in paths]
            await browser.close()
    finally:
        if tap is not None:
            await tap.stop()


@skip_no_browser
@pytest.mark.asyncio
async def test_page_tap_credits_each_navigation_to_its_own_entry_when_the_url_repeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = ["/a", "/b", "/a", "/c", "/a"]
    arrived = 0
    delivered = asyncio.Event()
    read_done = [asyncio.Event() for _ in paths]
    read_history = PageTap._enqueue_navigation_event

    async def read_in_order_after_every_navigation(
        self: PageTap, *args: Any, navigation_seq: int, **kwargs: Any
    ) -> None:
        # A busy event loop reads history only after later navigations are recorded, so hold every read until then.
        nonlocal arrived
        arrived += 1
        if arrived == len(paths):
            delivered.set()
        await delivered.wait()
        if navigation_seq > 1:
            await read_done[navigation_seq - 2].wait()
        try:
            await read_history(self, *args, navigation_seq=navigation_seq, **kwargs)
        finally:
            read_done[navigation_seq - 1].set()

    monkeypatch.setattr(PageTap, "_enqueue_navigation_event", read_in_order_after_every_navigation)
    tap: PageTap | None = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()

            loads: list[str] = []
            scripts = {
                # The first /a rewrites its URL and then pushes a state, which inherits the typed URL.
                ("/a", 1): "history.replaceState(null, '', '/a-rewritten'); history.pushState(null, '', '/a-pushed')",
                ("/b", 1): "history.replaceState(null, '', '/b-rewritten')",
            }

            async def serve(route: Any) -> None:
                path = urlparse(route.request.url).path
                loads.append(path)
                script = scripts.get((path, loads.count(path)), "")
                body = f"<html><body>Page<script>{script}</script></body></html>".encode()
                await route.fulfill(status=200, content_type="text/html", body=body)

            await context.route("**/*", serve)
            page = await context.new_page()
            await page.goto("http://main.test/")
            tap = PageTap(context)
            await tap.start([page])
            for path in paths:
                await page.goto(f"http://main.test{path}")
            await asyncio.wait_for(read_done[-1].wait(), timeout=10)

            navigations = [event.payload for event in tap.drain_events() if event.payload.get("kind") == "navigate"]
            assert [(payload["source_url"], payload["url"]) for payload in navigations] == [
                ("http://main.test/", "http://main.test/a-rewritten"),
                ("http://main.test/a", "http://main.test/b-rewritten"),
                ("http://main.test/b", "http://main.test/a"),
                ("http://main.test/a", "http://main.test/c"),
                ("http://main.test/c", "http://main.test/a"),
            ]
            await browser.close()
    finally:
        if tap is not None:
            await tap.stop()


@skip_no_browser
@pytest.mark.asyncio
async def test_page_tap_protects_labelled_secrets_and_captures_combobox_typing() -> None:
    tap: PageTap | None = None
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, channel=SELECT_TYPE_AHEAD_CHANNEL)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(
            """
            <label for="api-key">API key</label><input id="api-key">
            <label>Password <input id="wrapped-secret"></label>
            <span id="token-label">Access token</span><input id="aria-secret" aria-labelledby="token-label">
            <span>API key</span><span class="material-icons">visibility</span><svg aria-hidden="true"></svg><input id="icon-field">
            <div><label>Access key</label></div><div><input id="wrapper-field"></div>
            <h4>Secret key</h4><input id="heading-field">
            <dl><dt>API token</dt><dd><input id="dt-field"></dd></dl>
            <section>
              <label>Card number</label>
              <div><input id="card-1"><input id="card-2"><input id="card-3"><input id="card-4"></div>
            </section>
            <form><input type="hidden" name="authenticity_token" value="csrf-page-value"></form>
            <div><h2>Forgot your password?</h2><form><input id="reset-email" type="email"></form></div>
            <div>
              <h1>Reset password</h1>
              <div class="form-floating">
                <input id="floating-email" placeholder="name@example.com"><label for="floating-email">Email address</label>
              </div>
            </div>
            <div><h2>Two-factor authentication</h2><div><input id="tfa-phone" placeholder="Phone number"></div></div>
            <input id="early-toggle" type="password">
            <div><span>API key</span><input id="aria-labelled-email" aria-label="Email"></div>
            <div><span>Access token</span><form><input id="boundary-name" placeholder="Name"></form></div>
            <table><tr><td>Secret key</td><td><input id="table-field"></td></tr></table>
            <input id="password" type="password">
            <div id="page-copied-toggle">
              <input id="copied-source" type="password">
              <input id="copied-plain" type="text" hidden>
            </div>
            <div id="two-input-toggle">
              <input id="toggle-password" type="password">
              <input id="toggle-visible" type="text" hidden>
            </div>
            <section><label>API key</label><button>Copy</button><input id="copy-field"></section>
            <section>
              <div><label>API token</label><input id="api-token-row"></div>
              <div><input id="placeholder-name" placeholder="Full name"></div>
            </section>
            <section>
              <div><label>Card number</label><input id="card-row"></div>
              <div><input id="city" placeholder="City"></div>
            </section>
            <section>
              <p>Questions about your account number or PIN? Call us any time.</p>
              <input id="search-bills" placeholder="Search bills">
            </section>
            <div id="secret-shadow-host"></div>
            <script>
              document.querySelector('#secret-shadow-host').attachShadow({mode: 'open'}).innerHTML =
                '<label>API key <input id="shadow-secret"></label>';
            </script>
            <section id="identifier-first">
              <input id="username-first" type="text">
              <input id="hidden-login-password" type="password" hidden>
            </section>
            <label for="secret-select">API key</label>
            <select id="secret-select"><option value="">Choose</option><option value="select-sensitive-value">Secret</option></select>
            <input id="normal">
            <input id="combo" role="combobox" aria-controls="suggestions">
            <div id="suggestions"><button id="suggestion">John Smith</button></div>
            """
        )
        try:
            tap = PageTap(context)
            await tap.start([page])

            for selector, value in (
                ("#api-key", "api-sensitive-value"),
                ("#wrapped-secret", "wrapped-sensitive-value"),
                ("#aria-secret", "aria-sensitive-value"),
                ("#icon-field", "icon-sensitive-value"),
                ("#wrapper-field", "wrapper-sensitive-value"),
                ("#table-field", "table-sensitive-value"),
                ("#copy-field", "copy-sensitive-value"),
                ("#heading-field", "heading-sensitive-value"),
                ("#dt-field", "dt-sensitive-value"),
                ("#card-2", "box-sensitive-4242"),
                ("#tfa-phone", "tfa-sensitive-5550100"),
                ("#boundary-name", "boundary-sensitive-value"),
            ):
                await page.locator(selector).focus()
                await page.keyboard.type(value)
                await page.keyboard.press("Tab")

            await page.locator("#password").focus()
            await page.keyboard.press("Control+Alt+@")
            await page.keyboard.press("Shift+A")
            await page.keyboard.type("password-sensitive-value")
            await page.keyboard.press("Tab")

            await page.locator("#copied-source").focus()
            await page.keyboard.type("page-copied-value")
            await page.keyboard.press("Tab")
            await page.evaluate(
                """() => {
                    const visible = document.querySelector('#copied-plain');
                    visible.value = document.querySelector('#copied-source').value;
                    visible.hidden = false;
                }"""
            )
            await page.locator("#toggle-password").focus()
            await page.keyboard.type("copied-password-value")
            await page.keyboard.press("Tab")
            await page.evaluate(
                """() => {
                    const password = document.querySelector('#toggle-password');
                    const visible = document.querySelector('#toggle-visible');
                    password.hidden = true;
                    visible.hidden = false;
                    visible.value = password.value;
                }"""
            )
            await page.locator("#toggle-visible").focus()
            await page.keyboard.type("-corrected")
            await page.keyboard.press("Tab")
            await page.locator("#secret-shadow-host").locator("#shadow-secret").focus()
            await page.keyboard.type("shadow-sensitive-value")
            await page.keyboard.press("Tab")
            await page.evaluate("() => { document.querySelector('#early-toggle').type = 'text'; }")
            await page.locator("#early-toggle").focus()
            await page.keyboard.type("early-toggle-value")
            await page.keyboard.press("Tab")
            await page.evaluate(
                """() => {
                    const late = Object.assign(document.createElement('input'), {id: 'late-toggle', type: 'password'});
                    document.body.append(late);
                    const host = document.createElement('div');
                    host.id = 'late-shadow-host';
                    document.body.append(host);
                    host.attachShadow({mode: 'open'}).innerHTML = '<div><input id="shadow-toggle" type="password"></div>';
                }"""
            )
            await page.evaluate(
                """() => {
                    document.querySelector('#late-toggle').type = 'text';
                    document.querySelector('#late-shadow-host').shadowRoot.querySelector('#shadow-toggle').type = 'text';
                }"""
            )
            await page.evaluate(
                """() => {
                    const host = document.createElement('div');
                    host.id = 'lazy-shadow-host';
                    document.body.append(host);
                }"""
            )
            await page.evaluate(
                """() => {
                    const root = document.querySelector('#lazy-shadow-host').attachShadow({mode: 'open'});
                    root.innerHTML = '<input id="lazy-toggle" type="password"><button id="lazy-show">Show</button>';
                    root.querySelector('#lazy-show').addEventListener('click', () => {
                        root.querySelector('#lazy-toggle').type = 'text';
                    });
                }"""
            )
            await page.locator("#lazy-shadow-host").locator("#lazy-show").click()
            for locator, value in (
                (page.locator("#lazy-shadow-host").locator("#lazy-toggle"), "lazy-toggle-value"),
                (page.locator("#late-toggle"), "late-toggle-value"),
                (page.locator("#late-shadow-host").locator("#shadow-toggle"), "shadow-toggle-value"),
            ):
                await locator.focus()
                await page.keyboard.type(value)
                await page.keyboard.press("Tab")
            ordinary_fields = {
                "#reset-email": "person@example.test",
                "#floating-email": "floating@example.test",
                "#aria-labelled-email": "aria@example.test",
                "#placeholder-name": "Taylor Example",
                "#city": "Springfield",
                "#search-bills": "electric",
                "#username-first": "operator@example.test",
            }
            for selector, value in ordinary_fields.items():
                await page.locator(selector).focus()
                await page.keyboard.type(value)
                await page.keyboard.press("Tab")

            await page.locator("#secret-select").focus()
            await page.keyboard.press("s")

            await page.locator("#normal").focus()
            await page.keyboard.press("Enter")

            await page.locator("#combo").focus()
            await page.keyboard.type("John")
            await page.click("#suggestion")
            _, events = await _wait_for_event(tap, kind="click", selector="#suggestion")
            await tap.flush()
            events.extend(tap.drain_events())
            payloads = [event.payload for event in events]

            secret_selectors = {payload.get("selector") for payload in payloads if payload.get("kind") == "secret"}
            assert {
                "#api-key",
                "#wrapped-secret",
                "#aria-secret",
                "#icon-field",
                "#wrapper-field",
                "#table-field",
                "#heading-field",
                "#dt-field",
                "#card-2",
                "#tfa-phone",
                "#boundary-name",
                "#password",
                "#toggle-password",
                "#toggle-visible",
                "#copy-field",
                "#early-toggle",
                "#late-toggle",
            } <= secret_selectors
            assert not secret_selectors & set(ordinary_fields)
            assert {
                (payload.get("selector"), payload.get("value")) for payload in payloads if payload.get("kind") == "type"
            } >= set(ordinary_fields.items())
            await tap.stop()
            assert await page.locator("#api-key").input_value() == ""
            assert await page.locator("#secret-select").input_value() == ""
            assert await page.locator("#secret-shadow-host").locator("#shadow-secret").input_value() == ""
            assert await page.locator("#city").input_value() == "Springfield"
            assert await page.locator("#copied-plain").input_value() == ""
            assert await page.locator("input[name=authenticity_token]").input_value() == "csrf-page-value"
            assert not any(
                payload.get("kind") == "type" and payload.get("selector") in secret_selectors for payload in payloads
            )
            assert not any(
                payload.get("kind") == "key" and payload.get("selector") == "#password" for payload in payloads
            )
            assert not any(payload.get("key") in {"Control+Alt+@", "Shift+A"} for payload in payloads)
            assert any(
                payload.get("kind") == "secret_select" and payload.get("selector") == "#secret-select"
                for payload in payloads
            )
            assert any(payload.get("kind") == "key" and payload.get("key") == "Enter" for payload in payloads)
            assert all(
                value not in repr(payloads)
                for value in (
                    "api-sensitive-value",
                    "wrapped-sensitive-value",
                    "aria-sensitive-value",
                    "icon-sensitive-value",
                    "wrapper-sensitive-value",
                    "copy-sensitive-value",
                    "shadow-sensitive-value",
                    "table-sensitive-value",
                    "heading-sensitive-value",
                    "dt-sensitive-value",
                    "box-sensitive-4242",
                    "tfa-sensitive-5550100",
                    "boundary-sensitive-value",
                    "early-toggle-value",
                    "late-toggle-value",
                    "shadow-toggle-value",
                    "lazy-toggle-value",
                    "password-sensitive-value",
                    "copied-password-value",
                    "corrected",
                    "select-sensitive-value",
                )
            )
            assert [
                (payload["kind"], payload.get("selector"), payload.get("value"))
                for payload in payloads
                if payload.get("selector") in {"#combo", "#suggestion"}
            ] == [("type", "#combo", "John"), ("click", "#suggestion", None)]
        finally:
            if tap is not None:
                await tap.stop()
            await browser.close()


@skip_no_browser
@pytest.mark.asyncio
async def test_page_tap_names_field_kind_only_from_type_and_autocomplete() -> None:
    tap: PageTap | None = None
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.set_content(
            """
            <div><input id="masked-onetime" type="password" autocomplete="one-time-code"></div>
            <div><input id="masked-csc" type="password" autocomplete="cc-csc"></div>
            <div><input id="masked-four" type="password" name="pin"></div>
            <div><input id="login-box" type="password" autocomplete="current-password"></div>
            <div><input id="autofill-off" name="company" autocomplete="new-password"></div>
            <div><input id="tel-reveal" type="password"></div>
            <div><input id="reused-box" type="password" placeholder="Email"></div>
            <div><input id="confirm-box" type="text"></div>
            <div><h3>Security code</h3><div><input id="w1"></div></div>
            <div><input id="w2" name="securityCode"></div>
            <fieldset><legend>Password</legend><div><input id="w3"></div></fieldset>
            <div><h2>API token</h2><div class="input-group"><input id="w4"><button>Copy</button></div></div>
            <div><h4>PIN</h4><div><input id="w5"></div></div>
            <div><h2>Enter your one-time code</h2><form><input id="w6" name="entry"></form></div>
            <div><span>CVV</span><form><input id="w7"></form></div>
            <div><span>PIN</span><input id="w8" aria-labelledby="missing-node"></div>
            <div><span>PIN</span><input id="w9"><label for="w9"></label></div>
            <div>
              <h2>Enter verification code</h2>
              <div><input id="d1" aria-label="Digit 1"><input id="d2" aria-label="Digit 2"><input id="d3"></div>
            </div>
            <form><h2>One-time code</h2><div><input id="w10" placeholder="Enter here"></div></form>
            <div>
              <h3>Verification code</h3><p>We sent a six-digit number to the phone you added earlier.</p>
              <div><input id="w11"></div>
            </div>
            <div><p>Account number: 1234</p><h3>Change email</h3><div><input id="o1" type="email"></div></div>
            <div><span>Password</span><h2>Contact details</h2><div><input id="o2" placeholder="Full name"></div></div>
            <form><h2>Forgot your password?</h2><input id="o3" type="email"></form>
            <div id="swap-wrap"><input id="swap-box" type="password"></div>
            <div id="rerender"><div><input id="rr-box" type="password"><button>Show</button></div></div>
            <div id="delayed"><input id="delayed-box" type="password"></div>
            <div id="lazy-host"></div><button id="outside-show">Show</button>
            <button id="done-marker">Done</button>
            """
        )
        try:
            tap = PageTap(page.context)
            await tap.start([page])
            await page.evaluate(
                """() => {
                    document.querySelector('#reused-box').type = 'email';
                    document.querySelector('#tel-reveal').type = 'tel';
                    document.querySelector('#swap-box').replaceWith(
                        Object.assign(document.createElement('input'), {id: 'swap-shown', type: 'text'}));
                    document.querySelector('#rerender').innerHTML =
                        '<div><input id="rr-shown" type="text"><button>Hide</button></div>';
                    document.querySelector('#delayed-box').remove();
                    const root = document.querySelector('#lazy-host').attachShadow({mode: 'open'});
                    root.innerHTML = '<input id="lazy-box" type="password">';
                    document.querySelector('#outside-show').addEventListener('click', () => {
                        root.querySelector('#lazy-box').type = 'text';
                    });
                }"""
            )
            await page.evaluate(
                """() => document.querySelector('#delayed').append(
                    Object.assign(document.createElement('input'), {id: 'delayed-shown', type: 'text'}))"""
            )
            await page.click("#outside-show")

            secret_fields = {
                "#masked-onetime": "totp",
                "#masked-csc": "other",
                "#masked-four": "other",
                "#login-box": "password",
                "#autofill-off": "other",
                "#tel-reveal": "other",
                "#swap-shown": "other",
                "#rr-shown": "other",
                "#delayed-shown": "other",
                **{f"#w{index}": "other" for index in range(1, 12)},
                "#d2": "other",
                "#d3": "other",
            }
            ordinary_fields = {
                "#reused-box": "same@example.test",
                "#confirm-box": "same@example.test",
                "#o1": "change@example.test",
                "#o2": "Taylor Example",
                "#o3": "forgot@example.test",
            }
            for selector in secret_fields:
                await page.locator(selector).focus()
                await page.keyboard.type(f"withheld{selector[1:]}")
                await page.keyboard.press("Tab")
            await page.locator("#lazy-host").locator("#lazy-box").focus()
            await page.keyboard.type("withheld-lazy")
            await page.keyboard.press("Tab")
            for selector, value in ordinary_fields.items():
                await page.locator(selector).focus()
                await page.keyboard.type(value)
                await page.keyboard.press("Tab")
            await page.click("#done-marker")
            _, events = await _wait_for_event(tap, kind="click", selector="#done-marker")
            await tap.flush()
            events.extend(tap.drain_events())
            payloads = [event.payload for event in events]

            secrets = {payload["selector"]: payload for payload in payloads if payload.get("kind") == "secret"}
            assert {selector: secrets[selector]["field"] for selector in secret_fields if selector in secrets} == (
                secret_fields
            )
            assert any(selector.endswith("#lazy-box") for selector in secrets)
            assert secrets["#d2"]["field_label"] == "Digit 2"
            assert secrets["#w10"]["field_label"] == "Enter here"
            typed = {
                (payload.get("selector"), payload.get("value")) for payload in payloads if payload.get("kind") == "type"
            }
            assert typed >= set(ordinary_fields.items())
            assert "withheld" not in repr([payload for payload in payloads if payload.get("kind") != "secret"])
            assert "withheld" not in repr(secrets)
        finally:
            if tap is not None:
                await tap.stop()
            await browser.close()


@skip_no_browser
@pytest.mark.asyncio
async def test_page_tap_click_snapshot_is_bounded_on_large_pages() -> None:
    tap: PageTap | None = None
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        links = '<a href="#">Same name</a>' * 10_000
        await page.set_content(f'<a id="target" href="#">Same name</a>{links}')
        try:
            tap = PageTap(context)
            await tap.start([page])
            box = await page.locator("#target").bounding_box()
            assert box is not None
            await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

            started = time.monotonic()
            await page.mouse.down()
            elapsed = time.monotonic() - started
            await page.mouse.up()

            await _wait_for_event(tap, kind="click", selector="#target")
            assert elapsed < 0.5
        finally:
            if tap is not None:
                await tap.stop()
            await browser.close()


@skip_no_browser
@pytest.mark.asyncio
async def test_page_tap_records_element_drags_and_a_gap_per_freehand_stroke() -> None:
    body = b"""<html><body style="margin:0">
      <div id="card" draggable="true" style="width:80px;height:40px">Card</div>
      <div id="lane" style="width:200px;height:80px" ondragover="event.preventDefault()">Done lane</div>
      <button id="handle" style="width:80px;height:40px">Handle</button>
      <button id="slot" style="width:80px;height:40px">Slot</button>
      <canvas id="pad" width="200" height="100"></canvas>
      <div id="list" style="width:200px;height:120px"><button id="row" style="width:80px;height:30px">Row</button></div>
      <div id="loose" draggable="true" style="width:80px;height:30px">Loose</div>
      <p id="plain" style="height:40px">Plain</p>
      <div id="col-a" style="width:120px;height:60px"><button id="mover" style="width:80px;height:30px">Mover</button></div>
      <div id="col-b" style="width:120px;height:60px"></div>
      <button id="after">After</button>
      <script>
        let ghost = null;
        document.querySelector('#handle').addEventListener('pointerdown', () => {
          ghost = document.querySelector('#handle').cloneNode(true);
          ghost.removeAttribute('id');
          ghost.style.position = 'fixed';
          document.body.append(ghost);
        });
        document.addEventListener('pointermove', (event) => {
          if (ghost) Object.assign(ghost.style, {left: event.clientX - 10 + 'px', top: event.clientY - 10 + 'px'});
        });
        document.addEventListener('pointerup', () => setTimeout(() => { if (ghost) ghost.remove(); ghost = null; }));
        let moving = false;
        document.querySelector('#mover').addEventListener('pointerdown', () => { moving = true; });
        document.addEventListener('pointermove', (event) => {
          const column = document.querySelector('#col-b');
          const box = column.getBoundingClientRect();
          if (moving && event.clientY > box.top && event.clientY < box.bottom) column.append(document.querySelector('#mover'));
        });
        document.addEventListener('pointerup', () => { moving = false; });
      </script>
    </body></html>"""
    tap: PageTap | None = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()

            async def serve(route: Any) -> None:
                await route.fulfill(status=200, content_type="text/html", body=body)

            await context.route("**/*", serve)
            page = await context.new_page()
            await page.goto("http://main.test/")
            tap = PageTap(context)
            await tap.start([page])

            async def drag(source: str, target_x: float, target_y: float) -> None:
                box = await page.locator(source).bounding_box()
                assert box is not None
                await page.mouse.move(box["x"] + 10, box["y"] + 10)
                await page.mouse.down()
                await page.mouse.move(target_x, target_y, steps=5)
                await page.mouse.up()

            await page.drag_and_drop("#card", "#lane")
            native, _ = await _wait_for_event(tap, kind="drag")
            assert (native.payload["selector"], native.payload["target_selector"]) == ("#card", "#lane")

            await page.evaluate("getSelection().selectAllChildren(document.querySelector('#plain'))")
            slot = await page.locator("#slot").bounding_box()
            assert slot is not None
            await drag("#handle", slot["x"] + 10, slot["y"] + 10)
            pointer, pointer_events = await _wait_for_event(tap, kind="drag", selector="#handle")
            assert pointer.payload["target_selector"] == "#slot"
            assert not any(event.payload.get("kind") == "click" for event in pointer_events)

            pad = await page.locator("#pad").bounding_box()
            assert pad is not None
            await drag("#pad", pad["x"] + 60, pad["y"] + 60)
            plain = await page.locator("#plain").bounding_box()
            assert plain is not None
            await drag("#pad", plain["x"] + 10, plain["y"] + 10)
            await page.click("#after")
            _, stroke_events = await _wait_for_event(tap, kind="click", selector="#after")
            assert [event.payload["kind"] for event in stroke_events] == ["freeform_drag", "freeform_drag", "click"]

            column = await page.locator("#col-b").bounding_box()
            assert column is not None
            await drag("#mover", column["x"] + 100, column["y"] + 50)
            moved, _ = await _wait_for_event(tap, kind="drag", selector="#mover")
            assert moved.payload["target_selector"] == "#col-b"
            tap.drain_events()

            listbox = await page.locator("#list").bounding_box()
            assert listbox is not None
            await drag("#row", listbox["x"] + 150, listbox["y"] + 100)
            await page.drag_and_drop("#loose", "#plain")
            await page.click("#after")
            _, unrecorded = await _wait_for_event(tap, kind="click", selector="#after")
            assert [(event.payload["kind"], event.payload.get("selector")) for event in unrecorded] == [
                ("freeform_drag", "#row"),
                ("freeform_drag", "#loose"),
                ("click", "#after"),
            ]
            await browser.close()
    finally:
        if tap is not None:
            await tap.stop()


@skip_no_browser
@pytest.mark.asyncio
async def test_page_tap_click_name_matches_the_browser_accessible_name() -> None:
    tap: PageTap | None = None
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(
            """<div role="tablist">
              <div role="tab" id="step-1"><div style="display:inline-flex">1</div>
                <span aria-hidden="true" style="font-size:0">person</span><div>Client Info</div></div>
              <div role="tab" id="step-2"><div style="display:inline-flex">2</div>
                <span aria-hidden="true" style="font-size:0">assignment</span><div>Policy Info</div></div>
            </div>
            <button id="split">Sub<span>mit</span> order</button>
            <button id="contents">Save<span style="display:contents">draft</span></button>
            <span id="hidden-ref" hidden>Archive <b>item</b></span>
            <button id="labelled" aria-labelledby="hidden-ref">x</button>
            <button id="hidden-gap">Send<span hidden>icon</span>now</button>
            <button id="icon-inline">Open<span aria-hidden="true">icon</span>file</button>
            <label hidden for="account">Account <span hidden>number</span></label><input id="account">
            <style>.shown { display: inline; }</style>
            <button id="hidden-shown">Keep<span hidden class="shown">going</span></button>
            <button id="child-ref">Print <span aria-labelledby="suffix"></span>
              <span id="suffix" hidden>receipt</span></button>
            <a href="#" id="child-label">Open <i aria-label="settings"></i></a>
            <button id="visible-ref">Print <span aria-labelledby="visible-suffix"></span>
              <span id="visible-suffix">slip</span></button>
            <button id="self-labelled-ref">Copy <span aria-labelledby="named-ref">x</span></button>
            <span id="named-ref" aria-label="link">internal</span>"""
        )
        try:
            tap = PageTap(context)
            await tap.start([page])
            await page.click("#step-2")
            click, _ = await _wait_for_event(tap, kind="click", selector="#step-2")
            tab_name = click.payload["packet"]["role_name"]["accessible_name"]
            assert "assignment" not in tab_name
            assert await page.get_by_role("tab", name=tab_name, exact=True).count() == 1

            for selector in (
                "#split",
                "#contents",
                "#labelled",
                "#hidden-gap",
                "#icon-inline",
                "#account",
                "#hidden-shown",
                "#child-ref",
                "#child-label",
                "#visible-ref",
                "#self-labelled-ref",
            ):
                role_name = await page.evaluate(scout_accessible_role_name_expression(selector))
                assert (
                    await page.get_by_role(role_name["role"], name=role_name["accessible_name"], exact=True).count()
                    == 1
                )
        finally:
            if tap is not None:
                await tap.stop()
            await browser.close()
