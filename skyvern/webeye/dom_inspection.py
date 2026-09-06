from playwright.async_api import ElementHandle, Frame, Locator, Page

from skyvern.webeye.utils.page import SkyvernFrame

_READ_CURRENT_URL = "() => document.location.href"
_READ_LOCATOR_TAG_NAME = "element => element.tagName"
_READ_LOCATOR_IS_CONTENT_EDITABLE = "element => element.isContentEditable"
_READ_RESOLVED_ANCHOR_HREF = "(element) => element instanceof HTMLAnchorElement ? element.href : null"
_READ_WHETHER_LINK_OR_BUTTON = "(element) => element.matches('a[href], button')"

# One generic, live observable of a control's selected/checked state. Mirrors agent-side resolvers in
# skyvern/webeye/actions/handler.py so a cached replay and an agent run reach the same decision on the same DOM
_READ_LOCATOR_SELECTED_STATE = """
(element) => {
  const ariaBoolean = (raw) => {
    if (raw === null || raw === undefined) { return null; }
    const value = raw.trim().toLowerCase();
    if (value === 'true') { return true; }
    if (value === 'false') { return false; }
    return null;
  };
  const nativeToggleState = (control) => {
    if (!(control instanceof HTMLInputElement)) { return null; }
    const type = (control.type || '').trim().toLowerCase();
    if (type === 'radio') { return control.checked; }
    if (type !== 'checkbox') { return null; }
    if (control.closest('[role="grid"], [role="treegrid"]') !== null) { return null; }
    return control.checked;
  };
  if (element instanceof HTMLLabelElement) {
    const control = element.control;
    if (!control) { return null; }
    if (!element.hasAttribute('for')) {
      const candidates = Array.from(
        element.querySelectorAll('button, input, meter, output, progress, select, textarea')
      ).filter((candidate) => !(candidate instanceof HTMLInputElement && candidate.type === 'hidden'));
      if (candidates.length !== 1 || control !== candidates[0]) { return null; }
    }
    return nativeToggleState(control);
  }
  if (element instanceof HTMLInputElement) { return nativeToggleState(element); }
  const checked = ariaBoolean(element.getAttribute('aria-checked'));
  if (checked !== null) { return checked; }
  const pressed = ariaBoolean(element.getAttribute('aria-pressed'));
  if (pressed !== null) { return pressed; }
  const selected = ariaBoolean(element.getAttribute('aria-selected'));
  if (selected !== true) { return selected; }
  if ((element.getAttribute('role') || '').trim().toLowerCase() !== 'option') { return true; }
  const multiselectable = element.closest('[aria-multiselectable]');
  if (multiselectable === null) { return null; }
  const value = (multiselectable.getAttribute('aria-multiselectable') || '').trim().toLowerCase();
  return value === 'true' ? true : null;
}
"""


async def read_current_url(frame: Page | Frame) -> str | None:
    value = await SkyvernFrame.evaluate(frame=frame, expression=_READ_CURRENT_URL)
    return value if isinstance(value, str) else None


async def read_locator_tag_name(locator: Locator, *, timeout: float | None = None) -> str | None:
    value = await locator.evaluate(_READ_LOCATOR_TAG_NAME, timeout=timeout)
    return value if isinstance(value, str) else None


async def read_locator_selected_state(locator: Locator, *, timeout: float | None = None) -> bool | None:
    value = await locator.evaluate(_READ_LOCATOR_SELECTED_STATE, timeout=timeout)
    return value if isinstance(value, bool) else None


async def read_locator_is_content_editable(locator: Locator, *, timeout: float | None = None) -> bool | None:
    value = await locator.evaluate(_READ_LOCATOR_IS_CONTENT_EDITABLE, timeout=timeout)
    return value if isinstance(value, bool) else None


async def read_resolved_anchor_href(frame: Page | Frame, element: ElementHandle) -> str | None:
    value = await SkyvernFrame.evaluate(frame=frame, expression=_READ_RESOLVED_ANCHOR_HREF, arg=element)
    return value if isinstance(value, str) else None


async def read_whether_link_or_button(frame: Page | Frame, element: ElementHandle) -> bool | None:
    value = await SkyvernFrame.evaluate(frame=frame, expression=_READ_WHETHER_LINK_OR_BUTTON, arg=element)
    return value if isinstance(value, bool) else None
