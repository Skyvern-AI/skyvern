"""Pre-submit capture: the last frames before a submit-shaped action survive to the artifact store.

The fixtures are synthetic. Each run goes through the real loop with the real browser tools so the
property is asserted at the loop entry point, not at the ring's seam.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import pytest
from structlog.testing import capture_logs

from skyvern.forge.agent import _taskv3_action_for_tool_call
from skyvern.forge.taskv3.loop import SubmitWatch, make_finish_tool, run_agent_tool_loop
from skyvern.forge.taskv3.pre_submit_capture import PreSubmitCaptureRing, PreSubmitFrame, is_run_sampled
from skyvern.forge.taskv3.tools import build_browser_tools, pending_marker
from tests.unit.test_taskv3_loop import _ScriptedCaller
from tests.unit.test_taskv3_tools import _skip_no_browser

FIELD_COUNT = 10
VALUES = {f"f{i}": f"synthetic-answer-{i:02d}" for i in range(FIELD_COUNT)}
TOGGLE_COUNT = 26  # 26 toggles + submit + 3 confirmation-page clicks = 30, every one submit-shaped
POST_SUBMIT_CLICKS = ["#close", "#return", "#confirm"]
RING_SIZE = 8


def _form_fixture() -> str:
    fields = "".join(
        f'<p><label for="f{i}">Question {i}</label><input id="f{i}" name="f{i}" type="text"></p>'
        for i in range(FIELD_COUNT)
    )
    toggles = "".join(f'<label><input type="checkbox" id="t{i}"> Option {i}</label>' for i in range(TOGGLE_COUNT))
    return f"""<html><body>
<div id="app"><form id="application" onsubmit="return false">{fields}{toggles}
<button id="submit" type="button">Submit application</button></form></div>
<script>
document.getElementById('submit').addEventListener('click', () => {{
  // An ATS confirmation page: the form and its values are gone from the DOM.
  document.getElementById('app').innerHTML =
    '<h1>Thanks, your application was received</h1><button id="close">Close</button>';
  document.getElementById('close').addEventListener('click', () => {{
    document.getElementById('app').innerHTML = '<p>Application status: received</p><button id="return">Return</button>';
    document.getElementById('return').addEventListener('click', () => {{
      document.getElementById('app').innerHTML = '<p>Your applications</p><button id="confirm">Confirm</button>';
      document.getElementById('confirm').addEventListener('click', () => {{
        // A dashboard the model can keep clicking around after the confirmation: every click is
        // submit-shaped to the loop, and none of these pages holds a form.
        let n = 0;
        const more = () => {{
          n += 1;
          document.getElementById('app').innerHTML = `<p>Done ${{n}}</p><button id="more${{n}}">More</button>`;
          document.getElementById(`more${{n}}`).addEventListener('click', more);
        }};
        more();
      }});
    }});
  }});
}});
</script></body></html>"""


def _two_step_fixture() -> str:
    return """<html><body><div id="app">
<form id="step1" onsubmit="return false"><p><label for="s1">Step one answer</label><input id="s1" type="text"></p>
<button id="next" type="button">Next</button></form>
<form id="step2" style="display:none" onsubmit="return false"><p><label for="s2">Step two answer</label><input id="s2" type="text"></p>
<button id="submit" type="button">Submit</button></form></div>
<script>
document.getElementById('next').addEventListener('click', () => {
  document.getElementById('step1').style.display = 'none';
  document.getElementById('step2').style.display = '';
});
document.getElementById('submit').addEventListener('click', () => {
  document.getElementById('app').innerHTML = '<h1>Received</h1>';
});
</script></body></html>"""


def _filled_pairs(html: bytes | None) -> dict[str, str]:
    """(label text -> value) for every labelled text input whose value attribute is set: the shape the
    offline prevalence query derives from the DOM."""
    text = (html or b"").decode("utf-8")
    pairs: dict[str, str] = {}
    for label_for, label_text in re.findall(r'<label for="([^"]+)">([^<]+)</label>', text):
        m = re.search(rf'<input id="{re.escape(label_for)}"[^>]*\svalue="([^"]*)"', text)
        if m and m.group(1):
            pairs[label_text] = m.group(1)
    return pairs


def _terminal_frame(frames: list[PreSubmitFrame]) -> PreSubmitFrame | None:
    """The offline query's pick: the latest frame with the most filled controls is the pre-submit one,
    which is robust to clicks the model makes after the terminal submit. A ring with no filled control
    in any frame is INDETERMINATE (None), never a confirmation page mistaken for the form."""
    best = max(reversed(frames), key=lambda f: len(_filled_pairs(f.html)))
    return best if _filled_pairs(best.html) else None


async def _run_loop(page: Any, script: list[list[tuple[str, dict[str, Any]]]], ring: PreSubmitCaptureRing) -> Any:
    async def _provider() -> Any:
        return page

    tools = build_browser_tools(_provider) + [make_finish_tool()]
    return await run_agent_tool_loop(
        llm_caller=_ScriptedCaller(script),
        system_prompt="sys",
        user_prompt="goal",
        tools=tools,
        max_turns=80,
        max_tool_calls=200,
        on_pre_action=ring.capture,
    )


async def _browser_page(html: str) -> Any:
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.set_content(html)
    return pw, browser, page


@_skip_no_browser
@pytest.mark.asyncio
async def test_thirty_click_form_with_post_submit_close_keeps_every_typed_value_next_to_its_label() -> None:
    pw, browser, page = await _browser_page(_form_fixture())
    try:

        async def _provider() -> Any:
            return page

        async def _shot(p: Any) -> bytes:
            assert p is page
            return await p.screenshot()

        ring = PreSubmitCaptureRing(_provider, _shot)
        script: list[list[tuple[str, dict[str, Any]]]] = [
            [("type", {"selector": f"#{k}", "text": v}) for k, v in VALUES.items()],
            *[[("click", {"selector": f"#t{i}"})] for i in range(TOGGLE_COUNT)],
            [("click", {"selector": "#submit"})],
            *[[("click", {"selector": sel})] for sel in POST_SUBMIT_CLICKS],
            [("finish", {"status": "completed", "reason": "submitted"})],
        ]
        outcome = await _run_loop(page, script, ring)
        assert outcome.status == "completed"
        assert ring.captured == 30  # every click was reported, none dropped
        frames = ring.frames
        # No selection in the loop: the last eight frames, the submit one four from the end.
        submit_ordinal = TOGGLE_COUNT + 1
        assert [f.ordinal for f in frames] == list(range(submit_ordinal - 4, submit_ordinal + 4))

        # The guarantee: the terminal-submit frame survives the post-submit click, intact.
        (submit_frame,) = (f for f in frames if f.meta["selector"] == "#submit")
        assert _filled_pairs(submit_frame.html) == {f"Question {i}": VALUES[f"f{i}"] for i in range(FIELD_COUNT)}
        assert submit_frame.screenshot and submit_frame.screenshot[:4] == b"\x89PNG"
        # The fixture really destroys the form: the frame taken before the post-submit close has none.
        assert _filled_pairs(frames[-1].html) == {}
        # And the offline query's pick lands on it.
        assert _terminal_frame(frames) is submit_frame

        written: list[tuple[str, int]] = []

        async def _write(kind: str, data: bytes) -> str:
            written.append((kind, len(data)))
            return f"art_{len(written)}"

        assert await ring.persist(_write) == 2 * RING_SIZE
        assert [k for k, _ in written] == ["html", "screenshot"] * RING_SIZE
        assert await ring.persist(_write) == 0  # idempotent: the loop's finally must not double-write
    finally:
        await browser.close()
        await pw.stop()


@_skip_no_browser
@pytest.mark.asyncio
async def test_nine_post_submit_clicks_evict_the_submit_frame_and_read_as_indeterminate() -> None:
    # The coverage limit: the terminal frame survives at most RING_SIZE - 1 later submit-shaped
    # actions. Past that the ring holds only confirmation pages, and the offline pick must say so
    # rather than return one of them.
    pw, browser, page = await _browser_page(_form_fixture())
    try:

        async def _provider() -> Any:
            return page

        ring = PreSubmitCaptureRing(_provider, None)
        post_submit = POST_SUBMIT_CLICKS + [f"#more{n}" for n in range(1, RING_SIZE + 2 - len(POST_SUBMIT_CLICKS))]
        assert len(post_submit) == RING_SIZE + 1
        script: list[list[tuple[str, dict[str, Any]]]] = [
            [("type", {"selector": f"#{k}", "text": v}) for k, v in VALUES.items()],
            [("click", {"selector": "#submit"})],
            *[[("click", {"selector": sel})] for sel in post_submit],
            [("finish", {"status": "completed", "reason": "submitted"})],
        ]
        outcome = await _run_loop(page, script, ring)
        assert outcome.status == "completed"
        assert ring.captured == RING_SIZE + 2
        frames = ring.frames
        assert len(frames) == RING_SIZE
        assert all(f.meta["selector"] != "#submit" for f in frames)
        assert all(_filled_pairs(f.html) == {} for f in frames)
        assert _terminal_frame(frames) is None
    finally:
        await browser.close()
        await pw.stop()


@_skip_no_browser
@pytest.mark.asyncio
async def test_two_step_form_yields_the_frame_before_the_terminal_submit() -> None:
    pw, browser, page = await _browser_page(_two_step_fixture())
    try:

        async def _provider() -> Any:
            return page

        ring = PreSubmitCaptureRing(_provider, None)
        script: list[list[tuple[str, dict[str, Any]]]] = [
            [("type", {"selector": "#s1", "text": "first-step-value"}), ("click", {"selector": "#next"})],
            [("type", {"selector": "#s2", "text": "second-step-value"}), ("click", {"selector": "#submit"})],
            [("finish", {"status": "completed", "reason": "submitted"})],
        ]
        outcome = await _run_loop(page, script, ring)
        assert outcome.status == "completed"
        frames = ring.frames
        assert [f.meta["selector"] for f in frames] == ["#next", "#submit"]
        # Latched-on-first would keep the "Next" frame, which cannot contain the step-two value.
        assert b"second-step-value" not in (frames[0].html or b"")
        terminal = _terminal_frame(frames)
        assert terminal is frames[-1]
        assert _filled_pairs(terminal.html) == {
            "Step one answer": "first-step-value",
            "Step two answer": "second-step-value",
        }
    finally:
        await browser.close()
        await pw.stop()


@_skip_no_browser
@pytest.mark.asyncio
async def test_run_without_a_submit_shaped_action_persists_nothing() -> None:
    pw, browser, page = await _browser_page(_form_fixture())
    try:

        async def _provider() -> Any:
            return page

        ring = PreSubmitCaptureRing(_provider, None)
        script: list[list[tuple[str, dict[str, Any]]]] = [
            [("observe", {})],
            [("type", {"selector": "#f0", "text": "only-typed"})],
            [("finish", {"status": "terminated", "reason": "no submit here"})],
        ]
        await _run_loop(page, script, ring)
        assert ring.frames == []

        async def _write(kind: str, data: bytes) -> str:
            raise AssertionError("nothing to write")

        assert await ring.persist(_write) == 0
    finally:
        await browser.close()
        await pw.stop()


class _FakePage:
    url = "https://example.test/apply"

    def __init__(self, html: str) -> None:
        self._html = html

    async def evaluate(self, _js: str, max_bytes: int = 0) -> dict[str, Any]:
        size = len(self._html.encode("utf-8"))
        over = size > max_bytes
        return {
            "html": None if over else self._html,
            "bytes": size,
            "filled": 0,
            "iframes": 0,
            "shadowHosts": 0,
            "error": None,
        }


@pytest.mark.asyncio
async def test_oversized_dom_is_a_counted_skip_that_keeps_the_screenshot() -> None:
    # 2.2M two-byte characters: under the cap as a str length, over it as UTF-8 bytes.
    page = _FakePage("<html>" + "é" * (2_200_000) + "</html>")

    async def _provider() -> Any:
        return page

    async def _shot(_page: Any) -> bytes:
        return b"\x89PNG-bytes"

    ring = PreSubmitCaptureRing(_provider, _shot)
    await ring.capture("click", {"selector": "#submit"})
    (frame,) = ring.frames
    assert frame.html is None
    assert frame.html_skipped_bytes > 4 * 1024 * 1024
    assert frame.screenshot == b"\x89PNG-bytes"

    written: list[str] = []
    documents: list[bytes] = []

    async def _write(kind: str, data: bytes) -> str:
        written.append(kind)
        documents.append(data)
        return "art"

    # A header-only HTML document keeps the pair positional and records the skip in-band.
    assert await ring.persist(_write) == 2
    assert written == ["html", "screenshot"]
    assert documents[0].startswith(b"<!-- skyvern pre_submit_frame ordinal=1 tool=click ")
    assert b"dom_skipped_bytes=" in documents[0] and b"dom_skipped_bytes=0" not in documents[0]
    assert len(documents[0]) < 1024  # the oversized DOM never crossed the browser boundary


@pytest.mark.asyncio
async def test_persisted_html_carries_the_frame_header() -> None:
    page = _FakePage('<html><body><input id="a" value="typed"></body></html>')

    async def _provider() -> Any:
        return page

    ring = PreSubmitCaptureRing(_provider, None)
    await ring.capture("press_key", {"key": "Enter"})
    captured: list[bytes] = []

    async def _write(kind: str, data: bytes) -> str:
        captured.append(data)
        return "art"

    await ring.persist(_write)
    text = captured[0].decode()
    assert text.startswith("<!-- skyvern pre_submit_frame ordinal=1 tool=press_key ")


def test_frame_header_cannot_be_closed_by_page_influenced_text() -> None:
    # `--!>` closes an HTML comment just like `-->`; url and error both carry page text.
    frame = PreSubmitFrame(
        ordinal=1,
        tool_name="click",
        url="https://example.test/?q=--!><img src=x onerror=alert(1)>",
        captured_at=0.0,
        html=None,
        screenshot=None,
        error="RuntimeError: --><script>1</script>",
    )
    document = frame.html_document().decode()
    header, _, body = document.partition("-->\n")
    assert body == ""
    assert "--!>" not in header and "--><" not in header
    assert "onerror" in header  # neutralised, not dropped: the forensic value of the text survives


@pytest.mark.asyncio
async def test_a_type_that_presses_enter_discloses_that_a_text_is_pending() -> None:
    page = _FakePage('<html><body><input id="q"></body></html>')

    async def _provider() -> Any:
        return page

    ring = PreSubmitCaptureRing(_provider, None)
    await ring.capture("type", {"selector": "#q", "text": "hunter-synthetic", "press_enter": True})
    (frame,) = ring.frames
    assert frame.filled == 0 and frame.pending_text
    document = frame.html_document().decode()
    assert "pending_text=1" in document and "hunter-synthetic" not in document


@pytest.mark.asyncio
async def test_persist_writes_the_newest_frame_first_so_a_failing_write_costs_the_oldest() -> None:
    page = _FakePage('<html><body><input id="a" value="typed"></body></html>')

    async def _provider() -> Any:
        return page

    ring = PreSubmitCaptureRing(_provider, None)
    for _ in range(3):
        await ring.capture("click", {})
    ordinals: list[int] = []

    async def _write(kind: str, data: bytes) -> str | None:
        ordinals.append(int(re.search(rb"ordinal=(\d+)", data).group(1)))  # type: ignore[union-attr]
        if len(ordinals) == 2:
            raise RuntimeError("budget spent")
        return "art"

    assert await ring.persist(_write) == 2
    assert ordinals == [3, 2, 1]


def test_run_sampling_is_deterministic_and_respects_the_rate() -> None:
    keys = [f"tsk_{i}" for i in range(4000)]
    assert all(is_run_sampled(k, 1.0) for k in keys)
    assert not any(is_run_sampled(k, 0.0) for k in keys)
    sampled = sum(is_run_sampled(k, 0.25) for k in keys)
    assert 0.2 * len(keys) < sampled < 0.3 * len(keys)
    assert [is_run_sampled(k, 0.25) for k in keys[:50]] == [is_run_sampled(k, 0.25) for k in keys[:50]]


class _StalledPage(_FakePage):
    async def evaluate(self, _js: str, max_bytes: int = 0) -> dict[str, Any]:
        await asyncio.sleep(60)
        return {}


@pytest.mark.asyncio
async def test_a_wedged_page_costs_a_bounded_wait_and_keeps_the_screenshot() -> None:
    page = _StalledPage("")

    async def _provider() -> Any:
        return page

    async def _shot(_page: Any) -> bytes:
        return b"\x89PNG-bytes"

    ring = PreSubmitCaptureRing(_provider, _shot, step_timeout_seconds=0.05)
    started = time.monotonic()
    await ring.capture("click", {"selector": "#submit"})
    assert time.monotonic() - started < 1
    (frame,) = ring.frames
    assert frame.html is None and frame.screenshot == b"\x89PNG-bytes"


@_skip_no_browser
@pytest.mark.asyncio
async def test_capture_reads_live_values_without_mutating_the_page() -> None:
    html = """<html><body><form>
<p><label for="a">Answer</label><input id="a" type="text"></p>
<input id="pw" type="password"><textarea id="ta"></textarea>
<iframe srcdoc="<input id='inner'>"></iframe></form></body></html>"""
    pw, browser, page = await _browser_page(html)
    try:
        await page.fill("#a", "typed-live")
        await page.fill("#pw", "secret-live")
        await page.fill("#ta", "long-live")
        await page.evaluate("document.getElementById('a').setAttribute('data-tv3-pre', '1')")

        async def _provider() -> Any:
            return page

        ring = PreSubmitCaptureRing(_provider, None)
        await ring.capture("click", {"selector": "#submit"})
        (frame,) = ring.frames
        text = (frame.html or b"").decode()
        assert '<input id="a" type="text" value="typed-live">' in text
        assert '<textarea id="ta">long-live</textarea>' in text
        assert "secret-live" not in text
        assert "data-tv3" not in text
        assert frame.filled == 2 and frame.iframes == 1
        # The live page is untouched: no value attribute was written, the marker is still there.
        live = await page.evaluate(
            "() => [document.getElementById('a').getAttribute('value'),"
            " document.getElementById('a').getAttribute('data-tv3-pre'), document.getElementById('ta').textContent]"
        )
        assert live == [None, "1", ""]
    finally:
        await browser.close()
        await pw.stop()


@_skip_no_browser
@pytest.mark.asyncio
async def test_password_shaped_values_stay_out_and_the_match_over_includes_on_purpose() -> None:
    # Under-inclusion writes a plaintext password into a stored artifact; over-inclusion loses a data
    # point. So the name match is the broad substring /pass|pwd/ and passport/bypass lose their value.
    html = """<html><body>
<input id="password" type="password">
<input id="shown" name="login_secret" type="password" autocomplete="current-password">
<input id="fresh" name="login_secret_2" type="password" autocomplete="new-password">
<input id="confirm" name="confirm_password" type="text">
<input id="camel" name="newPassword" type="text">
<input id="loginPwd" type="text">
<input id="passwd" name="passwd" type="text">
<input id="phrase" name="passphrase" type="text">
<input id="code" name="passcode" type="text">
<input id="passport" name="passport_number" type="text">
<input id="bypass" name="bypass_reason" type="text">
<input id="answer" name="first_name" type="text">
<input id="attr_set" name="user_password" type="password">
<input id="default_value" name="account_password" type="password">
<input id="server_rendered" type="password" value="secret-server-rendered">
<textarea id="ta_secret" name="password_hint_passphrase"></textarea>
<div id="host"></div>
<script>document.getElementById('host').attachShadow({mode:'open'}).innerHTML = '<input id="inner">';</script>
</body></html>"""
    pw, browser, page = await _browser_page(html)
    try:
        secrets = {
            "#password": "secret-one",
            "#shown": "secret-two",
            "#fresh": "secret-three",
            "#confirm": "secret-four",
            "#camel": "secret-CAMEL",
            "#loginPwd": "secret-six",
            "#passwd": "secret-seven",
            "#phrase": "secret-eight",
            "#code": "secret-nine",
        }
        for selector, value in secrets.items():
            await page.fill(selector, value)
        await page.fill("#passport", "PASSPORT-SYNTHETIC-123")
        await page.fill("#bypass", "bypass-synthetic-value")
        await page.fill("#answer", "synthetic-first-name")
        # The value CONTENT ATTRIBUTE is serialised by outerHTML before any value is copied: a page
        # that mirrors the typed password into it (a controlled input does) must still lose it.
        await page.evaluate("document.getElementById('attr_set').setAttribute('value', 'secret-attr')")
        await page.evaluate("document.getElementById('default_value').defaultValue = 'secret-default'")
        await page.fill("#ta_secret", "secret-textarea")
        # A show-password toggle flips the type to text; only the autocomplete attribute still says
        # these two are passwords, since their names match nothing.
        await page.evaluate("for (const id of ['shown', 'fresh']) document.getElementById(id).type = 'text'")

        async def _provider() -> Any:
            return page

        ring = PreSubmitCaptureRing(_provider, None)
        await ring.capture("click", {})
        (frame,) = ring.frames
        text = (frame.html or b"").decode()
        for value in secrets.values():
            assert value not in text, value
        assert "PASSPORT-SYNTHETIC-123" not in text and "bypass-synthetic-value" not in text
        for leaked in ("secret-attr", "secret-default", "secret-server-rendered", "secret-textarea"):
            assert leaked not in text, leaked
        assert 'value="synthetic-first-name"' in text
        assert frame.filled == 1 and frame.shadow_hosts == 1
    finally:
        await browser.close()
        await pw.stop()


def _side_effect_fixture() -> str:
    """Every way a capture could touch the page it captures, each with a counter."""
    return """<html><head><style>
#mover { transition: transform 30s linear; transform: translateX(0); }
#mover.go { transform: translateX(500px); }
</style></head><body>
<div id="mover">moving</div>
<x-probe></x-probe>
<input type="image" src="http://synthetic.invalid/probe.png" alt="go"
  onload="window.__imgLoad++" onerror="window.__imgError++">
<p><label for="a">Answer</label><input id="a" type="text"></p>
<script>
window.__transitionEnd = 0; window.__ctor = 0; window.__mutations = 0; window.__imgLoad = 0; window.__imgError = 0;
document.getElementById('mover').addEventListener('transitionend', () => { window.__transitionEnd++; });
class Probe extends HTMLElement {
  constructor() {
    super();
    window.__ctor++;
    fetch('http://synthetic.invalid/api/ctor').catch(() => {});
    // A constructor that adds a child: an upgrade of a copy would change the control count.
    this.appendChild(document.createElement('input'));
  }
}
customElements.define('x-probe', Probe);
new MutationObserver((records) => { window.__mutations += records.length; })
  .observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
requestAnimationFrame(() => document.getElementById('mover').classList.add('go'));
</script></body></html>"""


_COUNTERS_JS = """() => ({
  transitionEnd: window.__transitionEnd, ctor: window.__ctor, mutations: window.__mutations,
  imgLoad: window.__imgLoad, imgError: window.__imgError,
  nodes: document.getElementsByTagName('*').length,
  x: getComputedStyle(document.getElementById('mover')).transform,
})"""


@_skip_no_browser
@pytest.mark.asyncio
async def test_a_capture_has_no_observable_side_effect_on_the_page_it_captures() -> None:
    """The invariant behind every "capture touches the page" finding: one full capture (DOM + the
    production screenshot) right before the submit click leaves transitions, constructors,
    mutations, loads, requests and node count exactly where they were."""
    from skyvern.forge.taskv3.pre_submit_capture import pre_submit_screenshot

    pw, browser, page = await _browser_page("<html></html>")
    requests: list[str] = []

    async def _route(route: Any) -> None:
        requests.append(route.request.url)
        await route.fulfill(status=200, body=b"", content_type="image/png")

    try:
        await page.route("http://synthetic.invalid/**", _route)
        await page.set_content(_side_effect_fixture())
        await page.fill("#a", "typed-live")
        await page.wait_for_function("() => window.__imgLoad + window.__imgError > 0")
        await page.wait_for_function("() => document.getElementById('mover').classList.contains('go')")
        await page.wait_for_timeout(100)
        # A 30 s transition must be in flight now, not at either end.
        before = await page.evaluate(_COUNTERS_JS)
        assert before["ctor"] == 1 and before["transitionEnd"] == 0
        assert before["x"] != "none" and before["x"] != "matrix(1, 0, 0, 1, 500, 0)"
        baseline_requests = list(requests)
        # The observer has seen the page settle; from here every mutation is the capture's.
        await page.evaluate("() => { window.__mutations = 0; }")

        async def _provider() -> Any:
            return page

        ring = PreSubmitCaptureRing(_provider, pre_submit_screenshot)
        await ring.capture("click", {"selector": "#submit"})
        await page.wait_for_timeout(100)
        after = await page.evaluate(_COUNTERS_JS)
        (frame,) = ring.frames
        assert frame.html is not None and frame.screenshot is not None and frame.error is None
        assert 'value="typed-live"' in frame.html.decode()
        assert after["transitionEnd"] == 0, "screenshot fast-forwarded an in-flight transition"
        assert after["ctor"] == 1, "DOM copy re-ran a page-author constructor"
        assert after["mutations"] == 0, "capture mutated the live document"
        assert after["imgLoad"] == before["imgLoad"] and after["imgError"] == before["imgError"]
        assert after["nodes"] == before["nodes"]
        assert requests == baseline_requests, "capture caused a network request"
        assert after["x"] != "matrix(1, 0, 0, 1, 500, 0)", "transition was jumped to its end state"
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_a_dom_failure_is_named_in_the_frame_header_instead_of_reading_as_an_empty_page() -> None:
    class _BrokenPage(_FakePage):
        async def evaluate(self, _js: str, max_bytes: int = 0) -> dict[str, Any]:
            return {"error": "reparse mismatch: 3 live vs 2", "html": None, "filled": 0}

    page = _BrokenPage("")

    async def _provider() -> Any:
        return page

    async def _shot(_page: Any) -> bytes:
        return b"\x89PNG-bytes"

    ring = PreSubmitCaptureRing(_provider, _shot)
    await ring.capture("click", {})
    (frame,) = ring.frames
    assert frame.html is None and frame.screenshot is not None
    header = frame.html_document().decode()
    assert "reparse mismatch: 3 live vs 2" in header and "filled=0" in header


_MARK_SUBMIT_FIXTURE = """<html><body>
<button id="submit" type="button">Submit application</button>
<label><input type="checkbox" id="agree"> Agree to terms</label>
<script>
document.getElementById('submit').addEventListener('click', (e) => {
  // The submission stays in flight: the control remains in the DOM and renames itself, which is the
  // only state the STOP-before-submit deferral has anything to say about.
  e.target.textContent = 'Submitting...';
});
</script></body></html>"""


def _mark_for(look_content: str, needle: str) -> int:
    line = next(ln for ln in look_content.splitlines() if needle in ln and ln.startswith("["))
    return int(line.split("]")[0].lstrip("["))


async def _run_with_submit_watch(page: Any, script: list[list[tuple[str, dict[str, Any]]]], watch: Any) -> Any:
    async def _provider() -> Any:
        return page

    async def _probe(selector: str) -> str | None:
        return await pending_marker(page, selector)

    tools = build_browser_tools(_provider) + [make_finish_tool(pending_marker=_probe, submit_watch=watch)]
    return await run_agent_tool_loop(
        llm_caller=_ScriptedCaller(script),
        system_prompt="sys",
        user_prompt="goal",
        tools=tools,
        max_turns=20,
        max_tool_calls=50,
        submit_watch=watch,
    )


@_skip_no_browser
@pytest.mark.asyncio
async def test_a_mark_click_on_a_submit_still_in_flight_holds_the_completed_verdict() -> None:
    # STOP-before-submit was blind to act-by-mark: the wrapper resolved the mark into a private copy,
    # so the loop's _names_submit_control saw no selector, recorded no watch, and a run that submitted
    # via a mark could call the job done while the page was still submitting. Real page, real probe,
    # real loop -- the deferral has to fire on the mark path exactly as it does on the selector path.
    pw, browser, page = await _browser_page(_MARK_SUBMIT_FIXTURE)
    try:
        watch = SubmitWatch()

        async def _provider() -> Any:
            return page

        looked = await next(t for t in build_browser_tools(_provider) if t.name == "look").handler({})
        submit_mark = _mark_for(looked.content, "Submit application")

        script = [
            [("look", {})],
            [("click", {"mark": submit_mark})],
            [("finish", {"status": "completed", "reason": "submitted"})],
            [("finish", {"status": "completed", "reason": "submitted"})],
        ]
        with capture_logs() as logs:
            outcome = await _run_with_submit_watch(page, script, watch)
    finally:
        await browser.close()
        await pw.stop()

    held = [e for e in logs if e["event"] == "taskv3 completed verdict held: submission still in flight"]
    assert len(held) == 1, [e["event"] for e in logs]
    assert held[0]["marker"].startswith("Submitting")
    assert outcome.status == "completed"  # the hold is one deferral, not a trap


@_skip_no_browser
@pytest.mark.asyncio
async def test_a_mark_click_on_a_non_submit_control_does_not_hold_the_verdict() -> None:
    # The other half of the discrimination: carrying the resolved selector must not make EVERY mark
    # click arm the deferral. A checkbox is clicked through the same wrapper and records the same
    # kind of watch; it is the probe, not the carrier, that decides, and it must stay silent here.
    pw, browser, page = await _browser_page(_MARK_SUBMIT_FIXTURE)
    try:
        watch = SubmitWatch()

        async def _provider() -> Any:
            return page

        looked = await next(t for t in build_browser_tools(_provider) if t.name == "look").handler({})
        agree_mark = _mark_for(looked.content, "Agree to terms")

        script = [
            [("look", {})],
            [("click", {"mark": agree_mark})],
            [("finish", {"status": "completed", "reason": "done"})],
        ]
        with capture_logs() as logs:
            outcome = await _run_with_submit_watch(page, script, watch)
    finally:
        await browser.close()
        await pw.stop()

    assert [e for e in logs if e["event"].startswith("taskv3 completed verdict held")] == []
    assert outcome.status == "completed"


@_skip_no_browser
@pytest.mark.asyncio
async def test_a_mark_click_persists_the_element_it_acted_on() -> None:
    # The measured bug this ticket was opened on: 2.4% of production v3 clicks persist element_id="".
    # They are the mark-based ones -- the loop appends the dispatched args to round_actions and the
    # action builder reads args["selector"] off it, so a wrapper that resolved into a copy left the
    # persisted row naming no element at all. Asserted through to the Action the row is built from.
    pw, browser, page = await _browser_page(_MARK_SUBMIT_FIXTURE)
    rounds: list[list[tuple[str, dict[str, Any], bool]]] = []

    async def _capture(actions: list[tuple[str, dict[str, Any], bool]], _text: str | None) -> None:
        rounds.append(actions)

    try:

        async def _provider() -> Any:
            return page

        looked = await next(t for t in build_browser_tools(_provider) if t.name == "look").handler({})
        submit_mark = _mark_for(looked.content, "Submit application")
        script = [
            [("look", {})],
            [("click", {"mark": submit_mark})],
            [("finish", {"status": "completed", "reason": "done"})],
        ]
        await run_agent_tool_loop(
            llm_caller=_ScriptedCaller(script),
            system_prompt="sys",
            user_prompt="goal",
            tools=build_browser_tools(_provider) + [make_finish_tool()],
            max_turns=20,
            max_tool_calls=50,
            on_action_round=_capture,
        )
    finally:
        await browser.close()
        await pw.stop()

    clicked = [(name, args) for round_ in rounds for name, args, ok in round_ if name == "click" and ok]
    assert len(clicked) == 1, rounds
    action = _taskv3_action_for_tool_call("click", clicked[0][1], reasoning="r")
    assert action.element_id, 'a mark-based click persisted element_id="" before the args carried it'
