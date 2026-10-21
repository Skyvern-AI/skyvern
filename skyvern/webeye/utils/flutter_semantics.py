"""Switch on a Flutter web app's accessibility semantics so its canvas-drawn interface gets a DOM to read."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from skyvern.webeye.browser_driver_errors import is_driver_error
from skyvern.webeye.browser_errors import BrowserTargetClosedError, is_target_closed_message

if TYPE_CHECKING:
    from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()

# One deadline spans every evaluate and sleep of an activation, so a page that never answers costs this and no more.
ACTIVATION_BUDGET_SECONDS = 3.0
_POLL_SECONDS = 0.25
# Room for one round trip to a remote browser: a poll started with less can only end in a page-analysis timeout, which
# counts against browser health.
_MIN_EVALUATE_SECONDS = 0.5
_ATTEMPT_MARK = "__semanticsActivationAttempted"

# The placeholder exists only while semantics are off, so finding it does not depend on any reader listing it. Older
# engines host the view in flt-glass-pane's open shadow root, so the known hosts' roots are searched as well.
_FIND_JS = """const roots = [document];
  const hosts = document.querySelectorAll('flutter-view, flt-glass-pane');
  for (let i = 0; i < hosts.length && i < 8; i++) {
    if (hosts[i].shadowRoot) roots.push(hosts[i].shadowRoot);
  }
  const find = (selector) => {
    for (const root of roots) {
      const match = root.querySelector(selector);
      if (match) return match;
    }
    return null;
  };"""

# An expression rather than a function, so a reader can embed it in the script it already runs.
FLUTTER_STATE_JS = f"""(() => {{
  try {{
    {_FIND_JS}
    return {{
      host: hosts.length > 0,
      placeholder: find('flt-semantics-placeholder') !== null,
      tree: find('flt-semantics') !== null,
    }};
  }} catch (e) {{
    return null;
  }}
}})()"""

_STATE_FN_JS = f"() => {FLUTTER_STATE_JS}"

# A text field of the app's accessibility tree. The engine starts editing it in the first frame after it is focused and
# then writes the framework's own copy of the value over the input, so text put there earlier is lost.
FLUTTER_TEXT_FIELD_JS = "(el) => !!el.closest('flt-semantics-host, [data-semantics-role=text-field]')"
# Resolves in the second frame from now, so the frame that handles an earlier focus or write has already run.
TWO_FRAMES_JS = "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done(true))))"
FRAME_SETTLE_TIMEOUT_SECONDS = 5.0

# One click per placeholder element: the engine listens for it from the moment it creates the placeholder, and a
# framework that starts later still picks up the enabled state, so an ignored click is not one that came too early. A
# rebuilt placeholder or a reload brings a new element to try, and the mobile engine deletes its placeholder after
# repeated misses.
_ACTIVATE_JS = f"""() => {{
  {_FIND_JS}
  const placeholder = hosts.length > 0 ? find('flt-semantics-placeholder') : null;
  // Read through the prototype, so a page that answers the query with some other element never gets clicked.
  const localName = Object.getOwnPropertyDescriptor(Element.prototype, 'localName').get;
  if (placeholder === null || localName.call(placeholder) !== 'flt-semantics-placeholder') return 'absent';
  if (placeholder['{_ATTEMPT_MARK}'] === true) return 'attempted';
  Object.defineProperty(placeholder, '{_ATTEMPT_MARK}', {{ value: true }});
  // The mobile engine only accepts a click within a pixel of the placeholder's centre; the desktop one takes any.
  const rect = placeholder.getBoundingClientRect();
  // The engine listens on the placeholder itself, so the click neither bubbles nor leaves a shadow root, and the
  // page's bubbling handlers and enclosing links never see it.
  placeholder.dispatchEvent(new MouseEvent('click', {{
    bubbles: false,
    cancelable: true,
    composed: false,
    view: window,
    clientX: rect.left + rect.width / 2,
    clientY: rect.top + rect.height / 2,
  }}));
  return 'dispatched';
}}"""


@dataclass
class FlutterSemanticsState:
    host: bool = False
    placeholder: bool = False
    tree: bool = False

    @classmethod
    def from_raw(cls, raw: Any) -> FlutterSemanticsState:
        state = cls()
        state.record(raw)
        return state

    def record(self, raw: Any) -> None:
        if isinstance(raw, dict):
            self.host = raw.get("host") is True
            self.placeholder = raw.get("placeholder") is True
            self.tree = raw.get("tree") is True

    @property
    def needs_activation(self) -> bool:
        # The current engine puts its placeholder on the body, outside its hosts, so the element's name alone does not
        # make a page an app.
        return self.host and self.placeholder

    @property
    def tree_missing(self) -> bool:
        return self.host and not self.tree


class SemanticsActivation(StrEnum):
    ACTIVATED = "activated"
    NOT_ACTIVATED = "not_activated"
    ALREADY_ATTEMPTED = "already_attempted"
    PLACEHOLDER_GONE = "placeholder_gone"
    FAILED = "failed"

    @property
    def reading_stale(self) -> bool:
        # A click, a failure partway through one, or a placeholder that vanished since the reading all mean the page
        # moved under it; only a placeholder already clicked in this document leaves it as it was read.
        return self is not SemanticsActivation.ALREADY_ATTEMPTED


async def activate_flutter_semantics(skyvern_frame: SkyvernFrame) -> SemanticsActivation:
    """Click the app's 'Enable accessibility' placeholder, then wait for its semantics tree, within one deadline."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + ACTIVATION_BUDGET_SECONDS
    try:
        answer = await _evaluate(skyvern_frame, _ACTIVATE_JS, deadline)
        if answer == "dispatched":
            activated = await _wait_for_tree(skyvern_frame, deadline)
            outcome = SemanticsActivation.ACTIVATED if activated else SemanticsActivation.NOT_ACTIVATED
        elif answer == "attempted":
            outcome = SemanticsActivation.ALREADY_ATTEMPTED
        else:
            outcome = SemanticsActivation.PLACEHOLDER_GONE
    except Exception as error:
        # A page, context or browser that went away is the reader's failure to report, not a failed activation.
        if _target_closed(skyvern_frame, error):
            raise
        LOG.warning("Could not switch on a Flutter app's accessibility semantics", exc_info=True)
        outcome = SemanticsActivation.FAILED
    LOG.info(
        "Flutter accessibility semantics activation",
        outcome=outcome.value,
        seconds=round(loop.time() - started, 2),
    )
    return outcome


async def _wait_for_tree(skyvern_frame: SkyvernFrame, deadline: float) -> bool:
    loop = asyncio.get_running_loop()
    while True:
        remaining = deadline - loop.time()
        if remaining <= _MIN_EVALUATE_SECONDS:
            return False
        await asyncio.sleep(min(_POLL_SECONDS, remaining - _MIN_EVALUATE_SECONDS))
        if FlutterSemanticsState.from_raw(await _evaluate(skyvern_frame, _STATE_FN_JS, deadline)).tree:
            return True


async def _evaluate(skyvern_frame: SkyvernFrame, expression: str, deadline: float) -> Any:
    return await skyvern_frame.evaluate(
        frame=skyvern_frame.frame,
        expression=expression,
        engine_selection=skyvern_frame.engine_selection,
        deadline=deadline,
    )


def _target_closed(skyvern_frame: SkyvernFrame, error: Exception) -> bool:
    engine_selection = skyvern_frame.engine_selection
    if engine_selection is None:
        return is_driver_error(error) and is_target_closed_message(str(error))
    if isinstance(engine_selection.classify_error(error), BrowserTargetClosedError):
        return True
    return engine_selection.is_engine_error(error) and is_target_closed_message(str(error))
