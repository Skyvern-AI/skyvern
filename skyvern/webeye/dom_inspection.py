from playwright.async_api import ElementHandle, Frame, Locator, Page

from skyvern.webeye.utils.page import SkyvernFrame

_READ_CURRENT_URL = "() => document.location.href"
_READ_LOCATOR_TAG_NAME = "element => element.tagName"
_READ_LOCATOR_IS_CONTENT_EDITABLE = "element => element.isContentEditable"
_READ_RESOLVED_ANCHOR_HREF = "(element) => element instanceof HTMLAnchorElement ? element.href : null"
_READ_WHETHER_LINK_OR_BUTTON = "(element) => element.matches('a[href], button')"

# Mirrors the driver's own label-following retarget plus fill()'s type allowlist, so this answers for the
# node a fill()/clear() would really land on. An editability check follows a <label> to its control too,
# which is why a radio/checkbox/file label reports editable while the typing it gates is always rejected.
_READ_LOCATOR_ACCEPTS_TEXT_FILL = """
(element) => {
  const FILLABLE_INPUT_TYPES = new Set([
    '', 'color', 'date', 'datetime-local', 'email', 'month', 'number',
    'password', 'range', 'search', 'tel', 'text', 'time', 'url', 'week',
  ]);
  let node = element;
  if (!node.matches('input, textarea, select') && !node.isContentEditable) {
    node = node.closest('button, [role=button], [role=checkbox], [role=radio]') || node;
  }
  const retargeted = 'a, input, textarea, button, select, [role=link], [role=button], [role=checkbox], [role=radio]';
  if (!node.matches(retargeted) && !node.isContentEditable) {
    const label = node.closest('label');
    if (label !== null && label.control) { node = label.control; }
  }
  const tag = (node.tagName || '').toLowerCase();
  if (tag === 'input') { return FILLABLE_INPUT_TYPES.has((node.type || '').toLowerCase()); }
  if (tag === 'textarea') { return true; }
  return node.isContentEditable === true;
}
"""

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

_READ_STATIC_LISTBOX_OPTION_SNAPSHOT = r"""
(el) => {
  const empty = {
    isOption: false,
    inListbox: false,
    popupSemanticsNearby: false,
    ownedByTrigger: false,
    activeDescendantRef: false,
    layered: false,
    textEntrySibling: false,
    openPopupOnPage: false,
  };
  try {
    const parentOf = (n) => n.parentElement || (n.getRootNode() instanceof ShadowRoot ? n.getRootNode().host : null);
    const roots = [];
    for (let r = el.getRootNode(); r; r = (r instanceof ShadowRoot ? r.host.getRootNode() : null)) roots.push(r);
    const isOption = (el.getAttribute('role') || '').trim().toLowerCase() === 'option';
    const listbox = el.closest('[role="listbox"]');
    if (listbox === null) { return Object.assign({}, empty, { isOption: isOption }); }

    const hasPopupSelector = '[aria-haspopup]:not([aria-haspopup="false"]):not([aria-haspopup=""])';
    const popupSelector = '[role="combobox"], [aria-expanded], ' + hasPopupSelector;
    let popupSemanticsNearby = false;
    for (let node = el; node !== null; node = parentOf(node)) {
      if (node.matches(popupSelector)) { popupSemanticsNearby = true; break; }
      if (node === listbox) {
        let ancestor = parentOf(listbox);
        for (let i = 0; i < 3 && ancestor !== null; i++, ancestor = parentOf(ancestor)) {
          if (ancestor.matches(popupSelector)) { popupSemanticsNearby = true; break; }
        }
        break;
      }
    }

    const optionId = el.id;
    const triggerSelector = popupSelector
      + ', button, input, select, textarea, [role="button"], [role="textbox"], [role="searchbox"]';
    const ownedByTrigger = roots.some((root) =>
      Array.from(root.querySelectorAll('[aria-controls], [aria-owns]')).some((owner) =>
        owner.matches(triggerSelector)
        && ['aria-controls', 'aria-owns'].some((attribute) =>
          (owner.getAttribute(attribute) || '').trim().split(/\s+/).filter(Boolean).some((token) => {
            const node = root.getElementById(token);
            return node === listbox
              || node === el
              || (node !== null && node.contains(listbox) && owner.matches(popupSelector));
          })
        )
      )
    );

    const activeDescendantRef = optionId !== '' && roots.some((root) =>
      Array.from(root.querySelectorAll('[aria-activedescendant]')).some((owner) =>
        owner.getAttribute('aria-activedescendant') === optionId
      )
    );

    const dialogSelector = 'dialog, [role="dialog"], [role="alertdialog"], [aria-modal="true"]';
    const hasPopoverBeforeDialog = function (node) {
      for (let ancestor = node; ancestor !== null; ancestor = parentOf(ancestor)) {
        if (ancestor.matches(dialogSelector)) { return false; }
        if (ancestor.matches('[popover]')) { return true; }
      }
      return false;
    };
    let layered = false;
    let layerNode = listbox;
    let inspectedNodes = 0;
    // Keep the window short: page-level positioning would disable the fix; stop at dialogs so modal forms stay static.
    for (; inspectedNodes < 4 && layerNode !== null; layerNode = parentOf(layerNode)) {
      if (layerNode.matches(dialogSelector)) { break; }
      const view = layerNode.ownerDocument.defaultView;
      const style = view ? view.getComputedStyle(layerNode) : null;
      if (style && style.display === 'contents') { continue; }
      inspectedNodes++;
      const position = style ? style.position : '';
      if (position === 'fixed' || position === 'absolute' || hasPopoverBeforeDialog(layerNode)) {
        layered = true;
        break;
      }
    }

    const parent = listbox.parentElement;
    const textEntrySibling = parent !== null && Array.from(parent.querySelectorAll(
      'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=button])'
        + ':not([type=submit]):not([type=reset]), textarea, [role="textbox"], [role="searchbox"], '
        + '[contenteditable="true"]'
    )).some((textEntry) => !listbox.contains(textEntry));

    const openPopupSelector = '[aria-expanded="true"][role="combobox"], [aria-expanded="true"]'
      + hasPopupSelector;
    const openPopupOnPage = roots.some((root) => root.querySelector(openPopupSelector) !== null);
    return {
      isOption: isOption,
      inListbox: true,
      popupSemanticsNearby: popupSemanticsNearby,
      ownedByTrigger: ownedByTrigger,
      activeDescendantRef: activeDescendantRef,
      layered: layered,
      textEntrySibling: textEntrySibling,
      openPopupOnPage: openPopupOnPage,
    };
  } catch (error) {
    return empty;
  }
}
"""

_MUST_BE_TRUE = ("isOption", "inListbox")
_MUST_BE_FALSE = (
    "popupSemanticsNearby",
    "ownedByTrigger",
    "activeDescendantRef",
    "layered",
    "textEntrySibling",
    "openPopupOnPage",
)


def is_static_listbox_option_snapshot(snapshot: object) -> bool:
    return (
        isinstance(snapshot, dict)
        and all(snapshot.get(key) is True for key in _MUST_BE_TRUE)
        and all(snapshot.get(key) is False for key in _MUST_BE_FALSE)
    )


async def read_current_url(frame: Page | Frame) -> str | None:
    value = await SkyvernFrame.evaluate(frame=frame, expression=_READ_CURRENT_URL)
    return value if isinstance(value, str) else None


async def read_locator_tag_name(locator: Locator, *, timeout: float | None = None) -> str | None:
    value = await locator.evaluate(_READ_LOCATOR_TAG_NAME, timeout=timeout)
    return value if isinstance(value, str) else None


async def read_locator_selected_state(locator: Locator, *, timeout: float | None = None) -> bool | None:
    value = await locator.evaluate(_READ_LOCATOR_SELECTED_STATE, timeout=timeout)
    return value if isinstance(value, bool) else None


async def read_static_listbox_option_snapshot(frame: Page | Frame, element: ElementHandle) -> object:
    return await SkyvernFrame.evaluate(frame=frame, expression=_READ_STATIC_LISTBOX_OPTION_SNAPSHOT, arg=element)


async def read_locator_accepts_text_fill(locator: Locator, *, timeout: float | None = None) -> bool | None:
    value = await locator.evaluate(_READ_LOCATOR_ACCEPTS_TEXT_FILL, timeout=timeout)
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
