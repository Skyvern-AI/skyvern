// we only use chromium browser for now
let browserNameForWorkarounds = "chromium";

let _jsConsoleLog = console?.log ?? function () {}; // prevent no console.log error
let _jsConsoleError = console?.error ?? _jsConsoleLog;
let _jsConsoleWarn = console?.warn ?? _jsConsoleLog;

class SafeCounter {
  constructor() {
    this.value = 0;
    this.lock = Promise.resolve();
  }

  async add() {
    await this.lock;
    this.lock = new Promise((resolve) => {
      this.value += 1;
      resolve();
    });
    return this.value;
  }

  async get() {
    await this.lock;
    return this.value;
  }
}

// Commands for manipulating rects.
// Want to debug this? Run chromium, go to sources, and create a new snippet with the code in domUtils.js
class Rect {
  // Create a rect given the top left and bottom right corners.
  static create(x1, y1, x2, y2) {
    return {
      bottom: y2,
      top: y1,
      left: x1,
      right: x2,
      width: x2 - x1,
      height: y2 - y1,
    };
  }

  static copy(rect) {
    return {
      bottom: rect.bottom,
      top: rect.top,
      left: rect.left,
      right: rect.right,
      width: rect.width,
      height: rect.height,
    };
  }

  // Translate a rect by x horizontally and y vertically.
  static translate(rect, x, y) {
    if (x == null) x = 0;
    if (y == null) y = 0;
    return {
      bottom: rect.bottom + y,
      top: rect.top + y,
      left: rect.left + x,
      right: rect.right + x,
      width: rect.width,
      height: rect.height,
    };
  }

  // Determine whether two rects overlap.
  static intersects(rect1, rect2) {
    return (
      rect1.right > rect2.left &&
      rect1.left < rect2.right &&
      rect1.bottom > rect2.top &&
      rect1.top < rect2.bottom
    );
  }

  static equals(rect1, rect2) {
    for (const property of [
      "top",
      "bottom",
      "left",
      "right",
      "width",
      "height",
    ]) {
      if (rect1[property] !== rect2[property]) return false;
    }
    return true;
  }
}

class DomUtils {
  static elementListCache = [];
  static visibleClientRectCache = new WeakMap();
  //
  // Bounds the rect by the current viewport dimensions. If the rect is offscreen or has a height or
  // width < 3 then null is returned instead of a rect.
  //
  static cropRectToVisible(rect) {
    const boundedRect = Rect.create(
      Math.max(rect.left, 0),
      Math.max(rect.top, 0),
      rect.right,
      rect.bottom,
    );
    if (
      boundedRect.top >= window.innerHeight - 4 ||
      boundedRect.left >= window.innerWidth - 4
    ) {
      return null;
    } else {
      return boundedRect;
    }
  }

  // add cache to optimize performance
  static getVisibleClientRect(element, testChildren = false) {
    // check cache
    const cacheKey = `${testChildren}`;
    if (DomUtils.visibleClientRectCache.has(element)) {
      const elementCache = DomUtils.visibleClientRectCache.get(element);
      if (elementCache.has(cacheKey)) {
        _jsConsoleLog("hit cache to get the rect of element");
        return elementCache.get(cacheKey);
      }
    }

    // Note: this call will be expensive if we modify the DOM in between calls.
    let clientRect;
    const clientRects = (() => {
      const result = [];
      for (clientRect of element.getClientRects()) {
        result.push(Rect.copy(clientRect));
      }
      return result;
    })();

    // Inline elements with font-size: 0px; will declare a height of zero, even if a child with
    // non-zero font-size contains text.
    let isInlineZeroHeight = function () {
      const elementComputedStyle = getElementComputedStyle(element, null);
      const isInlineZeroFontSize =
        0 ===
          elementComputedStyle?.getPropertyValue("display").indexOf("inline") &&
        elementComputedStyle?.getPropertyValue("font-size") === "0px";
      // Override the function to return this value for the rest of this context.
      isInlineZeroHeight = () => isInlineZeroFontSize;
      return isInlineZeroFontSize;
    };

    let result = null;

    for (clientRect of clientRects) {
      // If the link has zero dimensions, it may be wrapping visible but floated elements. Check for
      // this.
      let computedStyle;
      if ((clientRect.width === 0 || clientRect.height === 0) && testChildren) {
        for (const child of Array.from(element.children)) {
          computedStyle = getElementComputedStyle(child, null);
          if (!computedStyle) {
            continue;
          }
          // Ignore child elements which are not floated and not absolutely positioned for parent
          // elements with zero width/height, as long as the case described at isInlineZeroHeight
          // does not apply.
          // NOTE(mrmr1993): This ignores floated/absolutely positioned descendants nested within
          // inline children.
          const position = computedStyle.getPropertyValue("position");
          if (
            computedStyle.getPropertyValue("float") === "none" &&
            !["absolute", "fixed"].includes(position) &&
            !(
              clientRect.height === 0 &&
              isInlineZeroHeight() &&
              0 === computedStyle.getPropertyValue("display").indexOf("inline")
            )
          ) {
            continue;
          }
          const childClientRect = this.getVisibleClientRect(child, true);
          if (
            childClientRect === null ||
            childClientRect.width < 3 ||
            childClientRect.height < 3
          )
            continue;
          result = childClientRect;
          break;
        }
        if (result) break;
      } else {
        clientRect = this.cropRectToVisible(clientRect);

        if (
          clientRect === null ||
          clientRect.width < 3 ||
          clientRect.height < 3
        )
          continue;

        // eliminate invisible elements (see test_harnesses/visibility_test.html)
        computedStyle = getElementComputedStyle(element, null);
        if (!computedStyle) {
          continue;
        }
        if (computedStyle.getPropertyValue("visibility") !== "visible")
          continue;

        result = clientRect;
        break;
      }
    }

    // cache result
    if (!DomUtils.visibleClientRectCache.has(element)) {
      DomUtils.visibleClientRectCache.set(element, new Map());
    }
    DomUtils.visibleClientRectCache.get(element).set(cacheKey, result);

    return result;
  }

  // clear cache
  static clearVisibleClientRectCache() {
    DomUtils.visibleClientRectCache = new WeakMap();
  }

  static getViewportTopLeft() {
    const box = document.documentElement;
    const style = getComputedStyle(box);
    const rect = box.getBoundingClientRect();
    if (
      style &&
      style.position === "static" &&
      !/content|paint|strict/.test(style.contain || "")
    ) {
      // The margin is included in the client rect, so we need to subtract it back out.
      const marginTop = parseInt(style.marginTop);
      const marginLeft = parseInt(style.marginLeft);
      return {
        top: -rect.top + marginTop,
        left: -rect.left + marginLeft,
      };
    } else {
      const { clientTop, clientLeft } = box;
      return {
        top: -rect.top - clientTop,
        left: -rect.left - clientLeft,
      };
    }
  }
}

class QuadTreeNode {
  constructor(bounds, maxElements = 10, maxDepth = 4) {
    this.bounds = bounds; // {x, y, width, height}
    this.maxElements = maxElements;
    this.maxDepth = maxDepth;
    this.elements = [];
    this.children = null;
    this.depth = 0;
  }

  insert(element) {
    if (!this.contains(element.rect)) {
      return false;
    }

    if (this.children === null && this.elements.length < this.maxElements) {
      this.elements.push(element);
      return true;
    }

    if (this.children === null) {
      this.subdivide();
    }

    for (const child of this.children) {
      if (child.insert(element)) {
        return true;
      }
    }

    this.elements.push(element);
    return true;
  }

  subdivide() {
    const x = this.bounds.x;
    const y = this.bounds.y;
    const w = this.bounds.width / 2;
    const h = this.bounds.height / 2;

    this.children = [
      new QuadTreeNode(
        { x, y, width: w, height: h },
        this.maxElements,
        this.maxDepth,
      ),
      new QuadTreeNode(
        { x: x + w, y, width: w, height: h },
        this.maxElements,
        this.maxDepth,
      ),
      new QuadTreeNode(
        { x, y: y + h, width: w, height: h },
        this.maxElements,
        this.maxDepth,
      ),
      new QuadTreeNode(
        { x: x + w, y: y + h, width: w, height: h },
        this.maxElements,
        this.maxDepth,
      ),
    ];

    for (const child of this.children) {
      child.depth = this.depth + 1;
    }
  }

  contains(rect) {
    return (
      rect.left >= this.bounds.x &&
      rect.right <= this.bounds.x + this.bounds.width &&
      rect.top >= this.bounds.y &&
      rect.bottom <= this.bounds.y + this.bounds.height
    );
  }

  query(rect) {
    const result = [];
    this.queryRecursive(rect, result);
    return result;
  }

  queryRecursive(rect, result) {
    if (!this.intersects(rect)) {
      return;
    }

    result.push(...this.elements);

    if (this.children) {
      for (const child of this.children) {
        child.queryRecursive(rect, result);
      }
    }
  }

  intersects(rect) {
    return (
      rect.left < this.bounds.x + this.bounds.width &&
      rect.right > this.bounds.x &&
      rect.top < this.bounds.y + this.bounds.height &&
      rect.bottom > this.bounds.y
    );
  }
}

// from playwright
function getElementComputedStyle(element, pseudo) {
  return element.ownerDocument && element.ownerDocument.defaultView
    ? element.ownerDocument.defaultView.getComputedStyle(element, pseudo)
    : undefined;
}

// from playwright: https://github.com/microsoft/playwright/blob/1b65f26f0287c0352e76673bc5f85bc36c934b55/packages/playwright-core/src/server/injected/domUtils.ts#L76-L98
function isElementStyleVisibilityVisible(element, style) {
  style = style ?? getElementComputedStyle(element);
  if (!style) return true;
  // Element.checkVisibility checks for content-visibility and also looks at
  // styles up the flat tree including user-agent ShadowRoots, such as the
  // details element for example.
  // All the browser implement it, but WebKit has a bug which prevents us from using it:
  // https://bugs.webkit.org/show_bug.cgi?id=264733
  // @ts-ignore
  if (
    Element.prototype.checkVisibility &&
    browserNameForWorkarounds !== "webkit"
  ) {
    if (!element.checkVisibility()) return false;
  } else {
    // Manual workaround for WebKit that does not have checkVisibility.
    const detailsOrSummary = element.closest("details,summary");
    if (
      detailsOrSummary !== element &&
      detailsOrSummary?.nodeName === "DETAILS" &&
      !detailsOrSummary.open
    )
      return false;
  }
  if (style.visibility !== "visible") return false;

  // TODO: support style.clipPath and style.clipRule?
  // if element is clipped with rect(0px, 0px, 0px, 0px), it means it's invisible on the page
  // FIXME: need a better algorithm to calculate the visible rect area, using (right-left)*(bottom-top) from rect(top, right, bottom, left)
  if (
    style.clip === "rect(0px, 0px, 0px, 0px)" ||
    style.clip === "rect(1px, 1px, 1px, 1px)"
  ) {
    return false;
  }

  return true;
}

function hasASPClientControl() {
  return typeof ASPxClientControl !== "undefined";
}

// Check if element is only visible on hover (e.g., hover-only buttons)
function isHoverOnlyElement(element) {
  // Check for common hover-only patterns in class names
  const className = element.className?.toString() ?? "";
  const parentClassName = element.parentElement?.className?.toString() ?? "";

  // Common hover-only class patterns
  if (
    className.includes("hover-") ||
    className.includes("-hover") ||
    parentClassName.includes("hover-") ||
    parentClassName.includes("-hover")
  ) {
    return true;
  }

  // Check if parent has hover-related attributes or classes that might reveal this element
  let parent = element.parentElement;
  let depth = 0;
  // Cap recursion to avoid walking the entire tree and bloating prompts
  const maxDepth = 5;
  while (parent && parent !== document.body && depth < maxDepth) {
    const parentClass = parent.className?.toString() ?? "";
    if (
      parentClass.includes("hover") ||
      parentClass.includes("card") ||
      parentClass.includes("item")
    ) {
      // This element might be revealed on parent hover
      return true;
    }
    parent = parent.parentElement;
    depth += 1;
  }

  return false;
}

// from playwright: https://github.com/microsoft/playwright/blob/1b65f26f0287c0352e76673bc5f85bc36c934b55/packages/playwright-core/src/server/injected/domUtils.ts#L100-L119
// NOTE: According this logic, some elements with aria-hidden won't be considered as invisible. And the result shows they are indeed interactable.
function isElementVisible(element) {
  // TODO: This is a hack to not check visibility for option elements
  // because they are not visible by default. We check their parent instead for visibility.
  if (
    element.tagName.toLowerCase() === "option" ||
    (element.tagName.toLowerCase() === "input" &&
      (element.type === "radio" || element.type === "checkbox"))
  )
    return element.parentElement && isElementVisible(element.parentElement);

  const className = element.className ? element.className.toString() : "";
  if (
    className.includes("select2-offscreen") ||
    className.includes("select2-hidden") ||
    className.includes("ui-select-offscreen")
  ) {
    return false;
  }

  const style = getElementComputedStyle(element);
  if (!style) return true;
  if (style.display === "contents") {
    // display:contents is not rendered itself, but its child nodes are.
    for (let child = element.firstChild; child; child = child.nextSibling) {
      if (
        child.nodeType === 1 /* Node.ELEMENT_NODE */ &&
        isElementVisible(child)
      )
        return true;
      if (child.nodeType === 3 /* Node.TEXT_NODE */ && isVisibleTextNode(child))
        return true;
    }
    return false;
  }
  if (!isElementStyleVisibilityVisible(element, style)) return false;
  const rect = element.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) {
    // Check if this element might be visible on hover before marking as invisible
    if (isHoverOnlyElement(element)) {
      return true;
    }
    return false;
  }

  // if the center point of the element is not in the page, we tag it as an non-interactable element
  // FIXME: sometimes there could be an overflow element blocking the default scrolling, making Y coordinate be wrong. So we currently only check for X
  const center_x = (rect.left + rect.width) / 2 + window.scrollX;
  if (center_x < 0) {
    return false;
  }
  // const center_y = (rect.top + rect.height) / 2 + window.scrollY;
  // if (center_x < 0 || center_y < 0) {
  //   return false;
  // }

  return true;
}

// from playwright: https://github.com/microsoft/playwright/blob/1b65f26f0287c0352e76673bc5f85bc36c934b55/packages/playwright-core/src/server/injected/domUtils.ts#L121-L127
function isVisibleTextNode(node) {
  // https://stackoverflow.com/questions/1461059/is-there-an-equivalent-to-getboundingclientrect-for-text-nodes
  const range = node.ownerDocument.createRange();
  range.selectNode(node);
  const rect = range.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) {
    return false;
  }

  // if the center point of the element is not in the page, we tag it as an non-interactable element
  // FIXME: sometimes there could be an overflow element blocking the default scrolling, making Y coordinate be wrong. So we currently only check for X
  const center_x = (rect.left + rect.width) / 2 + window.scrollX;
  if (center_x < 0) {
    return false;
  }
  // const center_y = (rect.top + rect.height) / 2 + window.scrollY;
  // if (center_x < 0 || center_y < 0) {
  //   return false;
  // }
  return true;
}

// from playwright: https://github.com/microsoft/playwright/blob/d685763c491e06be38d05675ef529f5c230388bb/packages/playwright-core/src/server/injected/domUtils.ts#L37-L44
function parentElementOrShadowHost(element) {
  if (element.parentElement) return element.parentElement;
  if (!element.parentNode) return;
  if (
    element.parentNode.nodeType === 11 /* Node.DOCUMENT_FRAGMENT_NODE */ &&
    element.parentNode.host
  )
    return element.parentNode.host;
}

// from playwright: https://github.com/microsoft/playwright/blob/d685763c491e06be38d05675ef529f5c230388bb/packages/playwright-core/src/server/injected/domUtils.ts#L46-L52
function enclosingShadowRootOrDocument(element) {
  let node = element;
  while (node.parentNode) node = node.parentNode;
  if (
    node.nodeType === 11 /* Node.DOCUMENT_FRAGMENT_NODE */ ||
    node.nodeType === 9 /* Node.DOCUMENT_NODE */
  )
    return node;
}

// from playwright: https://github.com/microsoft/playwright/blob/d685763c491e06be38d05675ef529f5c230388bb/packages/playwright-core/src/server/injected/injectedScript.ts#L799-L859
function expectHitTarget(hitPoint, targetElement) {
  const roots = [];

  // Get all component roots leading to the target element.
  // Go from the bottom to the top to make it work with closed shadow roots.
  let parentElement = targetElement;
  while (parentElement) {
    const root = enclosingShadowRootOrDocument(parentElement);
    if (!root) break;
    roots.push(root);
    if (root.nodeType === 9 /* Node.DOCUMENT_NODE */) break;
    parentElement = root.host;
  }

  // Hit target in each component root should point to the next component root.
  // Hit target in the last component root should point to the target or its descendant.
  let hitElement;
  for (let index = roots.length - 1; index >= 0; index--) {
    const root = roots[index];
    // All browsers have different behavior around elementFromPoint and elementsFromPoint.
    // https://github.com/w3c/csswg-drafts/issues/556
    // http://crbug.com/1188919
    const elements = root.elementsFromPoint(hitPoint.x, hitPoint.y);
    const singleElement = root.elementFromPoint(hitPoint.x, hitPoint.y);
    if (
      singleElement &&
      elements[0] &&
      parentElementOrShadowHost(singleElement) === elements[0]
    ) {
      const style = getElementComputedStyle(singleElement);
      if (style?.display === "contents") {
        // Workaround a case where elementsFromPoint misses the inner-most element with display:contents.
        // https://bugs.chromium.org/p/chromium/issues/detail?id=1342092
        elements.unshift(singleElement);
      }
    }
    if (
      elements[0] &&
      elements[0].shadowRoot === root &&
      elements[1] === singleElement
    ) {
      // Workaround webkit but where first two elements are swapped:
      // <host>
      //   #shadow root
      //     <target>
      // elementsFromPoint produces [<host>, <target>], while it should be [<target>, <host>]
      // In this case, just ignore <host>.
      elements.shift();
    }
    const innerElement = elements[0];
    if (!innerElement) break;
    hitElement = innerElement;
    if (index && innerElement !== roots[index - 1].host) break;
  }

  // Check whether hit target is the target or its descendant.
  const hitParents = [];
  while (hitElement && hitElement !== targetElement) {
    hitParents.push(hitElement);
    hitElement = parentElementOrShadowHost(hitElement);
  }
  if (hitElement === targetElement) return null;

  return hitParents[0] || document.documentElement;
}

function getChildElements(element) {
  if (element.childElementCount !== 0) {
    return Array.from(element.children);
  } else {
    return [];
  }
}

function isParent(parent, child) {
  return parent.contains(child);
}

function isSibling(el1, el2) {
  return el1.parentElement === el2.parentElement;
}

function getBlockElementUniqueID(element) {
  const rect = element.getBoundingClientRect();

  const hitElement = expectHitTarget(
    {
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
    },
    element,
  );

  if (!hitElement) {
    return ["", false];
  }

  return [hitElement.getAttribute("unique_id") ?? "", true];
}

function isHidden(element) {
  const style = getElementComputedStyle(element);
  if (style?.display === "none") {
    return true;
  }
  if (element.hidden) {
    if (
      style?.cursor === "pointer" &&
      element.tagName.toLowerCase() === "input" &&
      (element.type === "submit" || element.type === "button")
    ) {
      // there are cases where the input is a "submit" button and the cursor is a pointer but the element has the hidden attr.
      // such an element is not really hidden
      return false;
    }
    return true;
  }
  return false;
}

function isHiddenOrDisabled(element) {
  return isHidden(element) || element.disabled;
}

function isScriptOrStyle(element) {
  const tagName = element.tagName.toLowerCase();
  return tagName === "script" || tagName === "style";
}

function isReadonlyElement(element) {
  if (element.readOnly) {
    return true;
  }

  if (element.hasAttribute("readonly")) {
    return true;
  }

  if (element.hasAttribute("aria-readonly")) {
    // only aria-readonly="false" should be considered as "not readonly"
    return (
      element.getAttribute("aria-readonly").toLowerCase().trim() !== "false"
    );
  }

  return false;
}

function isDropdownRelatedElement(element) {
  const tagName = element.tagName?.toLowerCase();
  if (tagName === "select") {
    return true;
  }

  const role = element.getAttribute("role")?.toLowerCase();
  if (role === "option" || role === "listbox") {
    return true;
  }

  return false;
}

function hasAngularClickBinding(element) {
  return (
    element.hasAttribute("ng-click") || element.hasAttribute("data-ng-click")
  );
}

function hasWidgetRole(element) {
  const role = element.getAttribute("role");
  if (!role) {
    return false;
  }
  // https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Roles#2._widget_roles
  // Not all roles make sense for the time being so we only check for the ones that do
  if (role.toLowerCase().trim() === "textbox") {
    return !isReadonlyElement(element);
  }

  const widgetRoles = [
    "button",
    "link",
    "checkbox",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "radio",
    "tab",
    "combobox",
    "searchbox",
    "slider",
    "spinbutton",
    "switch",
    "gridcell",
    "option",
  ];
  return widgetRoles.includes(role.toLowerCase().trim());
}

function isTableRelatedElement(element) {
  const tagName = element.tagName.toLowerCase();
  return [
    "table",
    "caption",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
    "colgroup",
    "col",
  ].includes(tagName);
}

function isDOMNodeRepresentDiv(element) {
  if (element?.tagName?.toLowerCase() !== "div") {
    return false;
  }
  const style = getElementComputedStyle(element);
  const children = getChildElements(element);
  // flex usually means there are multiple elements in the div as a line or a column
  // if the children elements are not just one, we should keep it in the HTML tree to represent a tree structure
  if (style?.display === "flex" && children.length > 1) {
    return true;
  }
  return false;
}

function isHoverPointerElement(element, hoverStylesMap) {
  const tagName = element.tagName.toLowerCase();
  const elementClassName = element.className.toString();
  const elementCursor = getElementComputedStyle(element)?.cursor;
  if (elementCursor === "pointer") {
    return true;
  }

  // Check if element has hover styles that change cursor to pointer
  // This is to handle the case where an element's cursor is "auto", but resolves to "pointer" on hover
  if (elementCursor === "auto" || elementCursor === "default") {
    // TODO: we need a better algorithm to match the selector with better performance
    for (const [selector, styles] of hoverStylesMap) {
      let shouldMatch = false;
      for (const className of element.classList) {
        if (selector.includes(className)) {
          shouldMatch = true;
          break;
        }
      }
      if (shouldMatch || selector.includes(tagName)) {
        if (element.matches(selector) && styles.cursor === "pointer") {
          return true;
        }
      }
    }
  }

  // FIXME: hardcode to fix the bug about hover style now
  if (elementClassName.includes("hover:cursor-pointer")) {
    return true;
  }

  return false;
}

function isInteractableInput(element, hoverStylesMap) {
  const tagName = element.tagName.toLowerCase();
  if (tagName !== "input") {
    // let other checks decide
    return false;
  }
  // Browsers default to "text" when the type is not set or is invalid
  // Here's the list of valid types: https://developer.mozilla.org/en-US/docs/Web/HTML/Element/input#input_types
  // Examples of unrecognized types that we've seen and caused issues because we didn't mark them interactable:
  // "city", "state", "zip", "country"
  // That's the reason I (Kerem) removed the valid input types check
  var type = element.getAttribute("type")?.toLowerCase().trim() ?? "text";
  return isHoverPointerElement(element, hoverStylesMap) || type !== "hidden";
}

function isValidCSSSelector(selector) {
  try {
    document.querySelector(selector);
    return true;
  } catch (e) {
    return false;
  }
}

function isInteractable(element, hoverStylesMap) {
  if (!isElementVisible(element)) {
    return false;
  }

  if (isHidden(element)) {
    return false;
  }

  if (isScriptOrStyle(element)) {
    return false;
  }

  if (hasWidgetRole(element)) {
    return true;
  }

  // element with pointer-events: none should not be considered as interactable
  // but for elements which are disabled, we should not use this logic to test the interactable
  // https://developer.mozilla.org/en-US/docs/Web/CSS/pointer-events#none
  const elementPointerEvent = getElementComputedStyle(element)?.pointerEvents;
  if (elementPointerEvent === "none" && !element.disabled) {
    // Some CTAs stay hidden until the parent is hovered
    // When we can infer that the element is revealed on hover, keep it interactable so the agent
    // has a chance to hover the parent before clicking.
    if (!isHoverOnlyElement(element)) {
      return false;
    }
  }

  if (isInteractableInput(element, hoverStylesMap)) {
    return true;
  }

  const tagName = element.tagName.toLowerCase();
  if (tagName === "html") {
    return false;
  }

  if (tagName === "iframe") {
    return false;
  }

  if (tagName === "frameset") {
    return false;
  }

  if (tagName === "frame") {
    return false;
  }

  if (tagName === "a" && element.href) {
    return true;
  }

  // Check if the option's parent (select) is hidden or disabled
  if (tagName === "option" && isHiddenOrDisabled(element.parentElement)) {
    return false;
  }

  if (
    tagName === "button" ||
    tagName === "select" ||
    tagName === "option" ||
    tagName === "textarea"
  ) {
    return true;
  }

  if (tagName === "label" && element.control && !element.control.disabled) {
    return true;
  }

  if (
    element.hasAttribute("onclick") ||
    element.isContentEditable ||
    element.hasAttribute("jsaction")
  ) {
    return true;
  }

  const className = element.className?.toString() ?? "";

  if (tagName === "div" || tagName === "span") {
    if (hasAngularClickBinding(element)) {
      return true;
    }
    if (className.includes("blinking-cursor")) {
      return true;
    }
    // https://www.oxygenxml.com/dita/1.3/specs/langRef/technicalContent/svg-container.html
    // svg-container is usually used for clickable elements that wrap SVGs
    if (className.includes("svg-container")) {
      return true;
    }
  }

  // support listbox and options underneath it
  // div element should be checked here before the css pointer
  if (
    (tagName === "ul" || tagName === "div") &&
    element.hasAttribute("role") &&
    element.getAttribute("role").toLowerCase() === "listbox"
  ) {
    return true;
  }
  if (
    (tagName === "li" || tagName === "div") &&
    element.hasAttribute("role") &&
    element.getAttribute("role").toLowerCase() === "option"
  ) {
    return true;
  }

  if (
    tagName === "li" &&
    (className.includes("ui-menu-item") ||
      className.includes("dropdown-item") ||
      className === "option")
  ) {
    return true;
  }

  // google map address auto complete
  // https://developers.google.com/maps/documentation/javascript/place-autocomplete#style-autocomplete
  // demo: https://developers.google.com/maps/documentation/javascript/examples/places-autocomplete-addressform
  if (
    tagName === "div" &&
    className.includes("pac-item") &&
    element.closest('div[class*="pac-container"]')
  ) {
    return true;
  }

  if (
    tagName === "div" &&
    element.hasAttribute("aria-disabled") &&
    element.getAttribute("aria-disabled").toLowerCase() === "false"
  ) {
    return true;
  }

  if (tagName === "span" && element.closest('div[id*="dropdown-container"]')) {
    return true;
  }

  // FIXME: maybe we need to enable the pointer check for all elements?
  if (
    tagName === "div" ||
    tagName === "img" ||
    tagName === "span" ||
    tagName === "a" ||
    tagName === "i" ||
    tagName === "li" ||
    tagName === "p" ||
    tagName === "td" ||
    tagName === "svg" ||
    tagName === "strong" ||
    tagName === "h1" ||
    tagName === "h2" ||
    tagName === "h3" ||
    tagName === "h4" ||
    // sometime it's a customized element like <my-login-button>, we should check pointer style
    tagName.includes("button") ||
    tagName.includes("select") ||
    tagName.includes("option") ||
    tagName.includes("textarea")
  ) {
    if (isHoverPointerElement(element, hoverStylesMap)) {
      return true;
    }
  }

  if (hasASPClientControl() && tagName === "tr") {
    return true;
  }

  if (tagName === "div" && element.hasAttribute("data-selectable")) {
    return true;
  }

  try {
    if (window.jQuery && window.jQuery._data) {
      const events = window.jQuery._data(element, "events");
      if (events && "click" in events) {
        return true;
      }
    }
  } catch (e) {
    _jsConsoleError("Error getting jQuery click events:", e);
  }

  try {
    if (hasAngularClickEvent(element)) {
      return true;
    }
  } catch (e) {
    _jsConsoleError("Error checking angular click event:", e);
  }

  return false;
}

function isScrollable(element) {
  const scrollHeight = element.scrollHeight || 0;
  const clientHeight = element.clientHeight || 0;
  const scrollWidth = element.scrollWidth || 0;
  const clientWidth = element.clientWidth || 0;

  const hasScrollableContent =
    scrollHeight > clientHeight || scrollWidth > clientWidth;
  const hasScrollableOverflow = isScrollableOverflow(element);
  return hasScrollableContent && hasScrollableOverflow;
}

function isScrollableOverflow(element) {
  const style = getElementComputedStyle(element);
  if (!style) {
    return false;
  }
  return (
    style.overflow === "auto" ||
    style.overflow === "scroll" ||
    style.overflowX === "auto" ||
    style.overflowX === "scroll" ||
    style.overflowY === "auto" ||
    style.overflowY === "scroll"
  );
}

function isDatePickerSelector(element) {
  const tagName = element.tagName.toLowerCase();
  if (
    tagName === "button" &&
    element.getAttribute("data-testid")?.includes("date")
  ) {
    return true;
  }
  return false;
}

const isComboboxDropdown = (element) => {
  if (element.tagName.toLowerCase() !== "input") {
    return false;
  }
  const role = element.getAttribute("role")
    ? element.getAttribute("role").toLowerCase()
    : "";
  const haspopup = element.getAttribute("aria-haspopup")
    ? element.getAttribute("aria-haspopup").toLowerCase()
    : "";
  const readonly =
    element.getAttribute("readonly") &&
    element.getAttribute("readonly").toLowerCase() !== "false";
  const controls = element.hasAttribute("aria-controls");
  return role && haspopup && controls && readonly;
};

const isDivComboboxDropdown = (element) => {
  const tagName = element.tagName.toLowerCase();
  if (tagName !== "div") {
    return false;
  }
  const role = element.getAttribute("role")
    ? element.getAttribute("role").toLowerCase()
    : "";
  const haspopup = element.getAttribute("aria-haspopup")
    ? element.getAttribute("aria-haspopup").toLowerCase()
    : "";
  const controls = element.hasAttribute("aria-controls");
  return role === "combobox" && controls && haspopup;
};

const isDropdownButton = (element) => {
  const tagName = element.tagName.toLowerCase();
  const type = element.getAttribute("type")
    ? element.getAttribute("type").toLowerCase()
    : "";
  const haspopup = element.getAttribute("aria-haspopup")
    ? element.getAttribute("aria-haspopup").toLowerCase()
    : "";
  const hasExpanded = element.hasAttribute("aria-expanded");
  return (
    tagName === "button" &&
    type === "button" &&
    (hasExpanded || haspopup === "listbox")
  );
};

const isSelect2Dropdown = (element) => {
  const tagName = element.tagName.toLowerCase();
  const className = element.className.toString();
  const role = element.getAttribute("role")
    ? element.getAttribute("role").toLowerCase()
    : "";

  if (tagName === "a") {
    return className.includes("select2-choice");
  }

  if (tagName === "span") {
    return className.includes("select2-selection") && role === "combobox";
  }

  return false;
};

const isSelect2MultiChoice = (element) => {
  return (
    element.tagName.toLowerCase() === "input" &&
    element.className.toString().includes("select2-input")
  );
};

const isReactSelectDropdown = (element) => {
  return (
    element.tagName.toLowerCase() === "input" &&
    element.className.toString().includes("select__input") &&
    element.getAttribute("role") === "combobox"
  );
};

function isReadonlyInputDropdown(element) {
  const className = element.className?.toString() ?? "";
  return (
    element.tagName.toLowerCase() === "input" &&
    className.includes("custom-select") &&
    isReadonlyElement(element)
  );
}

function hasNgAttribute(element) {
  if (!element.attributes[Symbol.iterator]) {
    return false;
  }

  for (let attr of element.attributes) {
    if (attr.name.startsWith("ng-")) {
      return true;
    }
  }
  return false;
}

// TODO: it's a hack, should continue to optimize it
function hasAngularClickEvent(element) {
  const ctx = element.__ngContext__;
  const tView = ctx && ctx[1];
  if (!tView || !Array.isArray(tView.data)) {
    return false;
  }

  const tagName = element.tagName.toLowerCase();
  if (!tagName) {
    _jsConsoleLog("Element has no tag name: ", element);
    return false;
  }

  for (const tNode of tView.data) {
    if (!tNode || typeof tNode !== "object") continue;
    if (tNode.type !== 0 && tNode.type !== 2) continue; // 0: Element, 2: Container
    if (tNode.value && tagName !== tNode.value.toLowerCase()) continue;
    if (!Array.isArray(tNode.attrs)) continue;
    if (tNode.attrs.includes("click")) {
      return true;
    }
  }

  return false;
}

function isAngularMaterial(element) {
  if (!element.attributes[Symbol.iterator]) {
    return false;
  }

  for (let attr of element.attributes) {
    if (attr.name.startsWith("mat")) {
      return true;
    }
  }
  return false;
}

const isAngularDropdown = (element) => {
  if (!hasNgAttribute(element)) {
    return false;
  }

  if (element.type?.toLowerCase() === "search") {
    return false;
  }

  const tagName = element.tagName.toLowerCase();
  if (tagName === "input" || tagName === "span") {
    const ariaLabel = element.hasAttribute("aria-label")
      ? element.getAttribute("aria-label").toLowerCase()
      : "";
    return ariaLabel.includes("select") || ariaLabel.includes("choose");
  }

  return false;
};

const isAngularMaterialDatePicker = (element) => {
  if (!isAngularMaterial(element)) {
    return false;
  }

  const tagName = element.tagName.toLowerCase();
  if (tagName !== "input") return false;

  return (
    (element.closest("mat-datepicker") ||
      element.closest("mat-formio-date")) !== null
  );
};

function getPseudoContent(element, pseudo) {
  const pseudoStyle = getElementComputedStyle(element, pseudo);
  if (!pseudoStyle) {
    return null;
  }
  const content = pseudoStyle
    .getPropertyValue("content")
    .replace(/"/g, "")
    .trim();

  if (content === "none" || !content) {
    return null;
  }

  return content;
}

function hasBeforeOrAfterPseudoContent(element) {
  return (
    getPseudoContent(element, "::before") != null ||
    getPseudoContent(element, "::after") != null
  );
}

const checkParentClass = (className) => {
  const targetParentClasses = ["field", "entry"];
  for (let i = 0; i < targetParentClasses.length; i++) {
    if (className.includes(targetParentClasses[i])) {
      return true;
    }
  }
  return false;
};

function removeMultipleSpaces(str) {
  // Optimization: check for empty values early
  if (!str || str.length === 0) {
    return str;
  }

  // Optimization: check if contains multiple spaces to avoid unnecessary regex replacement
  if (
    str.indexOf("  ") === -1 &&
    str.indexOf("\t") === -1 &&
    str.indexOf("\n") === -1
  ) {
    return str;
  }

  return str.replace(/\s+/g, " ");
}

function cleanupText(text) {
  // Optimization: check for empty values early to avoid unnecessary processing
  if (!text || text.length === 0) {
    return "";
  }

  // Optimization: use more efficient string replacement
  let cleanedText = text;

  // Remove specific SVG error message
  if (cleanedText.includes("SVGs not supported by this browser.")) {
    cleanedText = cleanedText.replace(
      "SVGs not supported by this browser.",
      "",
    );
  }

  // Optimization: combine space processing and trim operations
  return removeMultipleSpaces(cleanedText).trim();
}

const checkStringIncludeRequire = (str) => {
  return (
    str.toLowerCase().includes("*") ||
    str.toLowerCase().includes("✱") ||
    str.toLowerCase().includes("require")
  );
};

const checkRequiredFromStyle = (element) => {
  const afterCustomStyle = getElementComputedStyle(element, "::after");
  if (afterCustomStyle) {
    const afterCustom = afterCustomStyle
      .getPropertyValue("content")
      .replace(/"/g, "");
    if (checkStringIncludeRequire(afterCustom)) {
      return true;
    }
  }

  if (!element.className || typeof element.className !== "string") {
    return false;
  }

  return element.className.toLowerCase().includes("require");
};

function checkDisabledFromStyle(element) {
  const className = element.className.toString().toLowerCase();
  if (className.includes("react-datepicker__day--disabled")) {
    return true;
  }
  return false;
}

function getVisibleText(element) {
  let visibleText = [];

  function collectVisibleText(node) {
    if (
      node.nodeType === Node.TEXT_NODE &&
      isElementVisible(node.parentElement)
    ) {
      const trimmedText = node.data.trim();
      if (trimmedText.length > 0) {
        visibleText.push(trimmedText);
      }
    } else if (node.nodeType === Node.ELEMENT_NODE && isElementVisible(node)) {
      for (let child of node.childNodes) {
        collectVisibleText(child);
      }
    }
  }

  collectVisibleText(element);
  return visibleText.join(" ");
}

// only get text from element itself
function getElementText(element) {
  if (element.nodeType === Node.TEXT_NODE) {
    return element.data.trim();
  }

  const childNodes = element.childNodes;
  const childNodesLength = childNodes.length;

  // If no child nodes, return empty string directly
  if (childNodesLength === 0) {
    return "";
  }

  const visibleText = [];
  let hasText = false;

  for (let i = 0; i < childNodesLength; i++) {
    const node = childNodes[i];
    if (node.nodeType === Node.TEXT_NODE) {
      const nodeText = node.data.trim();
      if (nodeText.length > 0) {
        visibleText.push(nodeText);
        hasText = true;
      }
    }
  }

  return hasText ? visibleText.join(";") : "";
}

function getSelectOptions(element) {
  const options = Array.from(element.options);
  const selectOptions = [];

  for (const option of options) {
    selectOptions.push({
      optionIndex: option.index,
      text: removeMultipleSpaces(option.textContent),
      value: removeMultipleSpaces(option.value),
    });
  }

  const selectedOption = element.querySelector("option:checked");
  if (!selectedOption) {
    return [selectOptions, ""];
  }

  return [selectOptions, removeMultipleSpaces(selectedOption.textContent)];
}

function getDOMElementBySkyvenElement(elementObj) {
  // if element has shadowHost set, we need to find the shadowHost element first then find the element
  if (elementObj.shadowHost) {
    let shadowHostEle = document.querySelector(
      `[unique_id="${elementObj.shadowHost}"]`,
    );
    if (!shadowHostEle) {
      _jsConsoleLog(
        "Could not find shadowHost element with unique_id: ",
        elementObj.shadowHost,
      );
      return null;
    }
    return shadowHostEle.shadowRoot.querySelector(
      `[unique_id="${elementObj.id}"]`,
    );
  }

  return document.querySelector(`[unique_id="${elementObj.id}"]`);
}

if (window.elementIdCounter === undefined) {
  window.elementIdCounter = new SafeCounter();
}

// generate a unique id for the element
// length is 4, the first character is from the frame index, the last 3 characters are from the counter,
async function uniqueId() {
  const characters =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  const base = characters.length;

  const extraCharacters = "~!@#$%^&*()-_+=";
  const extraBase = extraCharacters.length;

  let result = "";

  if (
    window.GlobalSkyvernFrameIndex === undefined ||
    window.GlobalSkyvernFrameIndex < 0
  ) {
    const randomIndex = Math.floor(Math.random() * extraBase);
    result += extraCharacters[randomIndex];
  } else {
    const c1 = window.GlobalSkyvernFrameIndex % base;
    result += characters[c1];
  }

  const countPart =
    (await window.elementIdCounter.add()) % (base * base * base);
  const c2 = Math.floor(countPart / (base * base));
  result += characters[c2];
  const c3 = Math.floor(countPart / base) % base;
  result += characters[c3];
  const c4 = countPart % base;
  result += characters[c4];

  return result;
}

async function buildElementObject(
  frame,
  element,
  interactable,
  purgeable = false,
) {
  var element_id = element.getAttribute("unique_id") ?? (await uniqueId());
  var elementTagNameLower = element.tagName.toLowerCase();
  element.setAttribute("unique_id", element_id);

  const attrs = {};
  if (element.attributes[Symbol.iterator]) {
    for (const attr of element.attributes) {
      var attrValue = attr.value;
      if (
        attr.name === "required" ||
        attr.name === "aria-required" ||
        attr.name === "checked" ||
        attr.name === "aria-checked" ||
        attr.name === "selected" ||
        attr.name === "aria-selected" ||
        attr.name === "readonly" ||
        attr.name === "aria-readonly" ||
        attr.name === "disabled" ||
        attr.name === "aria-disabled"
      ) {
        if (attrValue && attrValue.toLowerCase() === "false") {
          attrValue = false;
        } else {
          attrValue = true;
        }
      }
      attrs[attr.name] = attrValue;
    }
  } else {
    _jsConsoleWarn(
      "element.attributes is not iterable. element_id=" + element_id,
    );
  }

  if (
    checkDisabledFromStyle(element) &&
    !attrs["disabled"] &&
    !attrs["aria-disabled"]
  ) {
    attrs["disabled"] = true;
  }

  if (
    checkRequiredFromStyle(element) &&
    !attrs["required"] &&
    !attrs["aria-required"]
  ) {
    attrs["required"] = true;
  }

  // check DOM property of required/checked/selected/readonly/disabled
  // the value from the DOM property should be the top priority
  if (element.required !== undefined) {
    delete attrs["required"];
    delete attrs["aria-required"];
    if (element.required) {
      attrs["required"] = true;
    }
  }
  if (element.checked !== undefined) {
    delete attrs["checked"];
    delete attrs["aria-checked"];
    if (element.checked) {
      attrs["checked"] = true;
    } else if (
      elementTagNameLower === "input" &&
      (element.type === "checkbox" || element.type === "radio")
    ) {
      // checked property always exists for checkbox and radio elements
      attrs["checked"] = false;
    }
  }
  if (element.selected !== undefined) {
    delete attrs["selected"];
    delete attrs["aria-selected"];
    if (element.selected) {
      attrs["selected"] = true;
    }
  }
  if (element.readOnly !== undefined) {
    delete attrs["readonly"];
    delete attrs["aria-readonly"];
    if (element.readOnly) {
      attrs["readonly"] = true;
    }
  }
  if (element.disabled !== undefined) {
    delete attrs["disabled"];
    delete attrs["aria-disabled"];
    if (element.disabled) {
      attrs["disabled"] = true;
    }
  }

  if (elementTagNameLower === "input" || elementTagNameLower === "textarea") {
    if (element.type === "password") {
      attrs["value"] = element.value ? "*".repeat(element.value.length) : "";
    } else {
      attrs["value"] = element.value;
    }
  }

  let elementObj = {
    id: element_id,
    frame: frame,
    frame_index: window.GlobalSkyvernFrameIndex,
    interactable: interactable,
    hoverOnly: isHoverOnlyElement(element),
    tagName: elementTagNameLower,
    attributes: attrs,
    beforePseudoText: getPseudoContent(element, "::before"),
    text: getElementText(element),
    afterPseudoText: getPseudoContent(element, "::after"),
    children: [],
    // if purgeable is True, which means this element is only used for building the tree relationship
    purgeable: purgeable,
    // don't trim any attr of this element if keepAllAttr=True
    keepAllAttr:
      elementTagNameLower === "svg" || element.closest("svg") !== null,
    isSelectable:
      elementTagNameLower === "select" ||
      isDatePickerSelector(element) ||
      isDivComboboxDropdown(element) ||
      isDropdownButton(element) ||
      isAngularDropdown(element) ||
      isAngularMaterialDatePicker(element) ||
      isSelect2Dropdown(element) ||
      isSelect2MultiChoice(element) ||
      isReadonlyInputDropdown(element),
  };

  let isInShadowRoot = element.getRootNode() instanceof ShadowRoot;
  if (isInShadowRoot) {
    let shadowHostEle = element.getRootNode().host;
    let shadowHostId = shadowHostEle.getAttribute("unique_id");
    // assign shadowHostId to the shadowHost element if it doesn't have unique_id
    if (!shadowHostId) {
      shadowHostId = await uniqueId();
      shadowHostEle.setAttribute("unique_id", shadowHostId);
    }
    elementObj.shadowHost = shadowHostId;
  }

  // get options for select element or for listbox element
  let selectOptions = null;
  let selectedValue = "";
  if (elementTagNameLower === "select") {
    [selectOptions, selectedValue] = getSelectOptions(element);
  }

  if (selectOptions) {
    elementObj.options = selectOptions;
  }
  if (selectedValue) {
    elementObj.attributes["selected"] = selectedValue;
  }

  return elementObj;
}

// build the element tree for the body
async function buildTreeFromBody(
  frame = "main.frame",
  frame_index = undefined,
) {
  if (
    window.GlobalSkyvernFrameIndex === undefined &&
    frame_index !== undefined
  ) {
    window.GlobalSkyvernFrameIndex = frame_index;
  }
  const maxElementNumber = 15000;
  const elementsAndResultArray = await buildElementTree(
    document.documentElement,
    frame,
    false,
    undefined,
    maxElementNumber,
  );
  DomUtils.elementListCache = elementsAndResultArray[0];
  return elementsAndResultArray;
}

async function buildElementTree(
  starter = document.documentElement,
  frame,
  full_tree = false,
  hoverStylesMap = undefined,
  maxElementNumber = 0,
) {
  // Generate hover styles map at the start
  if (hoverStylesMap === undefined) {
    hoverStylesMap = await getHoverStylesMap();
  }

  if (window.GlobalEnableAllTextualElements === undefined) {
    window.GlobalEnableAllTextualElements = false;
  }

  var elements = [];
  var resultArray = [];

  async function processElement(
    element,
    parentId,
    parent_xpath,
    current_node_index,
  ) {
    if (element === null) {
      _jsConsoleLog("get a null element");
      return;
    }

    if (maxElementNumber > 0 && elements.length >= maxElementNumber) {
      _jsConsoleWarn(
        "Max element number reached, aborting the element tree building",
      );
      return;
    }

    const tagName = element.tagName?.toLowerCase();
    if (!tagName) {
      _jsConsoleLog("get a null tagName");
      return;
    }

    if (tagName === "head") {
      return;
    }

    // skip processing option element as they are already added to the select.options
    if (tagName === "option") {
      return;
    }

    let current_xpath = null;
    if (parent_xpath !== null) {
      // ignore the namespace, otherwise the xpath sometimes won't find anything, specially for SVG elements
      current_xpath =
        parent_xpath +
        "/" +
        '*[name()="' +
        tagName +
        '"]' +
        "[" +
        current_node_index +
        "]";
    }

    let shadowDOMchildren = [];
    // sometimes the shadowRoot is not visible, but the elements in the shadowRoot are visible
    if (element.shadowRoot) {
      shadowDOMchildren = getChildElements(element.shadowRoot);
    }
    const isVisible = isElementVisible(element);
    if (isVisible && !isHidden(element) && !isScriptOrStyle(element)) {
      let interactable = isInteractable(element, hoverStylesMap);
      let elementObj = null;
      let isParentSVG = null;
      if (interactable) {
        elementObj = await buildElementObject(frame, element, interactable);
      } else if (
        tagName === "frameset" ||
        tagName === "iframe" ||
        tagName === "frame"
      ) {
        elementObj = await buildElementObject(frame, element, interactable);
      } else if (element.shadowRoot) {
        elementObj = await buildElementObject(frame, element, interactable);
      } else if (isTableRelatedElement(element)) {
        // build all table related elements into skyvern element
        // we need these elements to preserve the DOM structure
        elementObj = await buildElementObject(frame, element, interactable);
      } else if (hasBeforeOrAfterPseudoContent(element)) {
        elementObj = await buildElementObject(frame, element, interactable);
      } else if (tagName === "svg") {
        elementObj = await buildElementObject(frame, element, interactable);
      } else if (
        (isParentSVG = element.closest("svg")) &&
        isParentSVG.getAttribute("unique_id")
      ) {
        // if element is the children of the <svg> with an unique_id
        elementObj = await buildElementObject(frame, element, interactable);
      } else if (tagName === "div" && isDOMNodeRepresentDiv(element)) {
        elementObj = await buildElementObject(frame, element, interactable);
      } else if (
        tagName === "embed" &&
        element.getAttribute("type")?.toLowerCase() === "application/pdf"
      ) {
        elementObj = await buildElementObject(
          frame,
          element,
          interactable,
          true,
        );
      } else if (
        getElementText(element).length > 0 &&
        getElementText(element).length <= 5000
      ) {
        if (window.GlobalEnableAllTextualElements) {
          // force all textual elements to be interactable
          interactable = true;
        }
        elementObj = await buildElementObject(frame, element, interactable);
      } else if (full_tree) {
        // when building full tree, we only get text from element itself
        // elements without text are purgeable
        elementObj = await buildElementObject(
          frame,
          element,
          interactable,
          true,
        );
        if (elementObj.text.length > 0) {
          elementObj.purgeable = false;
        }
      }

      if (elementObj) {
        elementObj.xpath = current_xpath;
        elements.push(elementObj);
        // If the element is interactable but has no interactable parent,
        // then it starts a new tree, so add it to the result array
        // and set its id as the interactable parent id for the next elements
        // under it
        if (parentId === null) {
          resultArray.push(elementObj);
        }
        // If the element is interactable and has an interactable parent,
        // then add it to the children of the parent
        else {
          // TODO: use dict/object so that we access these in O(1) instead
          elements
            .find((element) => element.id === parentId)
            .children.push(elementObj);
        }
        parentId = elementObj.id;
      }
    }

    const children = getChildElements(element);
    const xpathMap = new Map();

    for (let i = 0; i < children.length; i++) {
      const childElement = children[i];
      const tagName = childElement?.tagName?.toLowerCase();
      if (!tagName) {
        _jsConsoleLog("get a null tagName");
        continue;
      }
      let current_node_index = xpathMap.get(tagName);
      if (current_node_index == undefined) {
        current_node_index = 1;
      } else {
        current_node_index = current_node_index + 1;
      }
      xpathMap.set(tagName, current_node_index);
      await processElement(
        childElement,
        parentId,
        current_xpath,
        current_node_index,
      );
    }

    // FIXME: xpath won't work when the element is in shadow DOM
    for (let i = 0; i < shadowDOMchildren.length; i++) {
      const childElement = shadowDOMchildren[i];
      await processElement(childElement, parentId, null, 0);
    }
    return;
  }

  const trimDuplicatedText = (element) => {
    if (element.children.length === 0 && !element.options) {
      return;
    }

    // if the element has options, text will be duplicated with the option text
    if (element.options) {
      element.options.forEach((option) => {
        element.text = element.text.replace(option.text, "");
      });
    }

    // BFS to delete duplicated text
    element.children.forEach((child) => {
      // delete duplicated text in the tree
      element.text = element.text.replace(child.text, "");
      trimDuplicatedText(child);
    });

    // trim multiple ";"
    element.text = element.text.replace(/;+/g, ";");
    // trimleft and trimright ";"
    element.text = element.text.replace(new RegExp(`^;+|;+$`, "g"), "");
  };

  // some elements without children nodes should be removed out, such as <label>
  const removeOrphanNode = (results) => {
    const trimmedResults = [];
    for (let i = 0; i < results.length; i++) {
      const element = results[i];
      element.children = removeOrphanNode(element.children);
      if (element.tagName === "label") {
        const labelElement = document.querySelector(
          element.tagName + '[unique_id="' + element.id + '"]',
        );
        if (
          labelElement &&
          labelElement.childElementCount === 0 &&
          !labelElement.getAttribute("for") &&
          !element.text
        ) {
          continue;
        }
      }
      trimmedResults.push(element);
    }
    return trimmedResults;
  };

  let current_xpath = null;
  if (starter === document.documentElement) {
    current_xpath = "";
  }

  // setup before parsing the dom
  await processElement(starter, null, current_xpath, 1);

  for (var element of elements) {
    if (
      ((element.tagName === "input" && element.attributes["type"] === "text") ||
        element.tagName === "textarea") &&
      (element.attributes["required"] || element.attributes["aria-required"]) &&
      element.attributes.value === ""
    ) {
      // TODO (kerem): we may want to pass these elements to the LLM as empty but required fields in the future
      _jsConsoleLog(
        "input element with required attribute and no value",
        element,
      );
    }
  }

  resultArray = removeOrphanNode(resultArray);
  resultArray.forEach((root) => {
    trimDuplicatedText(root);
  });

  return [elements, resultArray];
}

function drawBoundingBoxes(elements) {
  // draw a red border around the elements
  DomUtils.clearVisibleClientRectCache();
  elements.forEach((element) => {
    const ele = getDOMElementBySkyvenElement(element);
    element.rect = ele ? DomUtils.getVisibleClientRect(ele, true) : null;
  });
  var groups = groupElementsVisually(elements);
  var hintMarkers = createHintMarkersForGroups(groups);
  addHintMarkersToPage(hintMarkers);
  DomUtils.clearVisibleClientRectCache();
}

async function buildElementsAndDrawBoundingBoxes(
  frame = "main.frame",
  frame_index = undefined,
) {
  if (DomUtils.elementListCache.length > 0) {
    drawBoundingBoxes(DomUtils.elementListCache);
    return;
  }
  _jsConsoleWarn("no element list cache, drawBoundingBoxes from scratch");
  var elementsAndResultArray = await buildTreeFromBody(frame, frame_index);
  drawBoundingBoxes(elementsAndResultArray[0]);
}

function captchaSolvedCallback() {
  _jsConsoleLog("captcha solved");
  if (!window["captchaSolvedCounter"]) {
    window["captchaSolvedCounter"] = 0;
  }
  // For some reason this isn't being called.. TODO figure out why
  window["captchaSolvedCounter"] = window["captchaSolvedCounter"] + 1;
}

function getCaptchaSolves() {
  if (!window["captchaSolvedCounter"]) {
    window["captchaSolvedCounter"] = 0;
  }
  return window["captchaSolvedCounter"];
}

function groupElementsVisually(elements) {
  // Quadtree O(n log n)
  const validElements = elements.filter((element) => element.rect);

  if (validElements.length === 0) return [];

  // Calculate bounds
  const bounds = calculateBounds(validElements);

  // Create quadtree
  const quadTree = new QuadTreeNode(bounds);
  validElements.forEach((element) => quadTree.insert(element));

  const groups = [];
  const processed = new Set();

  for (const element of validElements) {
    if (processed.has(element)) continue;

    const group = { elements: [element], rect: null };
    processed.add(element);

    // Find all elements overlapping with current element
    const overlapping = findOverlappingElements(
      element,
      validElements,
      quadTree,
      processed,
    );

    for (const overlappingElement of overlapping) {
      group.elements.push(overlappingElement);
      processed.add(overlappingElement);
    }

    group.rect = createRectangleForGroup(group);
    groups.push(group);
  }

  return groups;
}

// Helper functions
function calculateBounds(elements) {
  const rects = elements.map((el) => el.rect);
  const left = Math.min(...rects.map((r) => r.left));
  const top = Math.min(...rects.map((r) => r.top));
  const right = Math.max(...rects.map((r) => r.right));
  const bottom = Math.max(...rects.map((r) => r.bottom));

  return {
    x: left,
    y: top,
    width: right - left,
    height: bottom - top,
  };
}

function findOverlappingElements(element, allElements, quadTree, processed) {
  const result = [];
  const queue = [element];

  while (queue.length > 0) {
    const current = queue.shift();

    // Use quadtree to query nearby elements
    const nearby = quadTree.query(current.rect);

    for (const nearbyElement of nearby) {
      if (
        !processed.has(nearbyElement) &&
        nearbyElement !== current &&
        Rect.intersects(current.rect, nearbyElement.rect)
      ) {
        result.push(nearbyElement);
        processed.add(nearbyElement);
        queue.push(nearbyElement);
      }
    }
  }
  return result;
}

function createRectangleForGroup(group) {
  const rects = group.elements.map((element) => element.rect);
  const top = Math.min(...rects.map((rect) => rect.top));
  const left = Math.min(...rects.map((rect) => rect.left));
  const bottom = Math.max(...rects.map((rect) => rect.bottom));
  const right = Math.max(...rects.map((rect) => rect.right));
  return Rect.create(left, top, right, bottom);
}

function generateHintStrings(count) {
  const hintCharacters = "sadfjklewcmpgh";
  let hintStrings = [""];
  let offset = 0;

  while (hintStrings.length - offset < count || hintStrings.length === 1) {
    const hintString = hintStrings[offset++];
    for (const ch of hintCharacters) {
      hintStrings.push(ch + hintString);
    }
  }
  hintStrings = hintStrings.slice(offset, offset + count);

  // Shuffle the hints so that they're scattered; hints starting with the same character and short
  // hints are spread evenly throughout the array.
  return hintStrings.sort(); // .map((str) => str.reverse())
}

function createHintMarkersForGroups(groups) {
  if (groups.length === 0) {
    _jsConsoleLog("No groups found, not adding hint markers to page.");
    return [];
  }

  const hintMarkers = groups
    .filter((group) => group.elements.some((element) => element.interactable))
    .map((group) => createHintMarkerForGroup(group));
  // fill in marker text
  // const hintStrings = generateHintStrings(hintMarkers.length);
  for (let i = 0; i < hintMarkers.length; i++) {
    const hintMarker = hintMarkers[i];

    let interactableElementFound = false;

    for (let i = 0; i < hintMarker.group.elements.length; i++) {
      if (hintMarker.group.elements[i].interactable) {
        hintMarker.hintString = hintMarker.group.elements[i].id;
        interactableElementFound = true;
        break;
      }
    }

    if (!interactableElementFound) {
      hintMarker.hintString = "";
    }

    try {
      hintMarker.element.innerHTML = hintMarker.hintString;
    } catch (e) {
      // Ensure trustedTypes is available
      if (typeof trustedTypes !== "undefined") {
        try {
          const escapeHTMLPolicy = trustedTypes.createPolicy("hint-policy", {
            createHTML: (string) => string,
          });
          hintMarker.element.innerHTML = escapeHTMLPolicy.createHTML(
            hintMarker.hintString.toUpperCase(),
          );
        } catch (policyError) {
          _jsConsoleWarn("Could not create trusted types policy:", policyError);
          // Skip updating the hint marker if policy creation fails
        }
      } else {
        _jsConsoleError("trustedTypes is not supported in this environment.");
      }
    }
  }

  return hintMarkers;
}

function createHintMarkerForGroup(group) {
  // Calculate the position of the element relative to the document
  var scrollTop = window.pageYOffset || document.documentElement.scrollTop;
  var scrollLeft = window.pageXOffset || document.documentElement.scrollLeft;

  const marker = {};
  // yellow annotation box with string
  const el = document.createElement("div");
  el.style.position = "absolute";
  el.style.left = group.rect.left + scrollLeft + "px";
  el.style.top = group.rect.top + scrollTop + "px";
  // Each group is assigned a different incremental z-index, we use the same z-index for the
  // bounding box and the hint marker
  el.style.zIndex = this.currentZIndex;

  // The bounding box around the group of hints.
  const boundingBox = document.createElement("div");

  // Set styles for the bounding box
  boundingBox.style.position = "absolute";
  boundingBox.style.display = "display";
  boundingBox.style.left = group.rect.left + scrollLeft + "px";
  boundingBox.style.top = group.rect.top + scrollTop + "px";
  boundingBox.style.width = group.rect.width + "px";
  boundingBox.style.height = group.rect.height + "px";
  boundingBox.style.bottom = boundingBox.style.top + boundingBox.style.height;
  boundingBox.style.right = boundingBox.style.left + boundingBox.style.width;
  boundingBox.style.border = "2px solid blue"; // Change the border color as needed
  boundingBox.style.pointerEvents = "none"; // Ensures the box doesn't interfere with other interactions
  boundingBox.style.zIndex = this.currentZIndex++;

  return Object.assign(marker, {
    element: el,
    boundingBox: boundingBox,
    group: group,
  });
}

function addHintMarkersToPage(hintMarkers) {
  const parent = document.createElement("div");
  parent.id = "boundingBoxContainer";
  for (const hintMarker of hintMarkers) {
    parent.appendChild(hintMarker.element);
    parent.appendChild(hintMarker.boundingBox);
  }
  document.documentElement.appendChild(parent);
}

function removeBoundingBoxes() {
  var hintMarkerContainer = document.querySelector("#boundingBoxContainer");
  if (hintMarkerContainer) {
    hintMarkerContainer.remove();
  }
}

function safeWindowScroll(x, y) {
  if (typeof window.scroll === "function") {
    window.scroll({ left: x, top: y, behavior: "instant" });
  } else if (typeof window.scrollTo === "function") {
    window.scrollTo({ left: x, top: y, behavior: "instant" });
  } else {
    _jsConsoleError("window.scroll and window.scrollTo are both not supported");
  }
}

async function safeScrollToTop(
  draw_boxes,
  frame = "main.frame",
  frame_index = undefined,
) {
  removeBoundingBoxes();
  safeWindowScroll(0, 0);
  if (draw_boxes) {
    await buildElementsAndDrawBoundingBoxes(frame, frame_index);
  }
  return window.scrollY;
}

function getScrollWidthAndHeight() {
  return [
    document.documentElement.scrollWidth,
    document.documentElement.scrollHeight,
  ];
}

function getScrollXY() {
  return [window.scrollX, window.scrollY];
}

function scrollToXY(x, y) {
  safeWindowScroll(x, y);
}

async function scrollToNextPage(
  draw_boxes,
  frame = "main.frame",
  frame_index = undefined,
  need_overlap = true,
) {
  // remove bounding boxes, scroll to next page with 200px overlap, then draw bounding boxes again
  // return true if there is a next page, false otherwise
  removeBoundingBoxes();
  window.scrollBy({
    left: 0,
    top: need_overlap ? window.innerHeight - 200 : window.innerHeight,
    behavior: "instant",
  });
  if (draw_boxes) {
    await buildElementsAndDrawBoundingBoxes(frame, frame_index);
  }
  return window.scrollY;
}

function isWindowScrollable() {
  const documentBody = document.body;
  const documentElement = document.documentElement;
  if (!documentBody || !documentElement) {
    return false;
  }

  // Check if the body's overflow style is set to hidden
  const bodyOverflow = getElementComputedStyle(documentBody)?.overflow;
  const htmlOverflow = getElementComputedStyle(documentElement)?.overflow;

  // Check if the document height is greater than the window height
  const isScrollable =
    document.documentElement.scrollHeight > window.innerHeight;

  // If the overflow is set to 'hidden' or there is no content to scroll, return false
  if (bodyOverflow === "hidden" || htmlOverflow === "hidden" || !isScrollable) {
    return false;
  }

  return true;
}

function scrollToElementBottom(element, page_by_page = false) {
  const top = page_by_page
    ? element.clientHeight + element.scrollTop
    : element.scrollHeight;
  element.scroll({
    top: top,
    left: 0,
    behavior: "smooth",
  });
}

function scrollToElementTop(element) {
  element.scroll({
    top: 0,
    left: 0,
    behavior: "instant",
  });
}

/**
 * Get all styles associated with :hover selectors
 *
 * Chrome doesn't allow you to compute these in run-time because hover is a protected attribute (from JS code)
 *
 * Instead of checking the hover state, we can look at the stylesheet and find all the :hover selectors
 * and try to infer styles associated with them
 *
 * It's not 100% accurate, but it's a good start
 *
 * References:
 * https://stackoverflow.com/questions/23040926/how-can-i-get-elementhover-style
 * https://stackoverflow.com/questions/7013559/is-there-a-way-to-get-element-hover-style-while-the-element-not-in-hover-state
 * https://stackoverflow.com/questions/17226676/how-to-simulate-a-mouseover-in-pure-javascript-that-activates-the-css-hover
 */
async function getHoverStylesMap() {
  const hoverMap = new Map();
  const sheets = [...document.styleSheets];

  const parseCssSheet = (sheet) => {
    const rules = sheet.cssRules || sheet.rules;
    for (const rule of rules) {
      if (rule.type === 1 && rule.selectorText) {
        // Split multiple selectors (e.g., "a:hover, button:hover")
        const selectors = rule.selectorText.split(",").map((s) => s.trim());

        for (const selector of selectors) {
          // Check if this is a hover rule
          if (selector.includes(":hover")) {
            // Get all parts of the selector
            const parts = selector.split(/\s*[>+~]\s*/);

            // Get the main hoverable element (the one with :hover)
            const hoverPart = parts.find((part) => part.includes(":hover"));
            if (!hoverPart) continue;

            // Get base selector without :hover
            const baseSelector = hoverPart.replace(/:hover/g, "").trim();

            // Skip invalid selectors
            if (!isValidCSSSelector(baseSelector)) {
              continue;
            }

            // Get or create styles object for this selector
            let styles = hoverMap.get(baseSelector) || {};

            // Add all style properties
            for (const prop of rule.style) {
              styles[prop] = rule.style[prop];
            }

            // If this is a nested selector (like :hover > .something)
            // store it in a special format
            if (parts.length > 1) {
              const fullSelector = selector;
              styles["__nested__"] = styles["__nested__"] || [];
              styles["__nested__"].push({
                selector: fullSelector,
                styles: Object.fromEntries(
                  [...rule.style].map((prop) => [prop, rule.style[prop]]),
                ),
              });
            }

            // only need the style which includes the cursor attribute.
            if (!("cursor" in styles)) {
              continue;
            }
            hoverMap.set(baseSelector, styles);
          }
        }
      }
    }
  };

  try {
    await Promise.all(
      sheets.map(async (sheet) => {
        try {
          parseCssSheet(sheet);
        } catch (e) {
          _jsConsoleWarn("Could not access stylesheet:", e);

          if ((e.name !== "SecurityError" && e.code !== 18) || !sheet.href) {
            return;
          }

          let newLink = null;
          try {
            const oldLink = sheet.ownerNode;
            const url = new URL(sheet.href);
            _jsConsoleLog("recreating the link element: ", sheet.href);
            newLink = document.createElement("link");
            newLink.rel = "stylesheet";
            url.searchParams.set("v", Date.now());
            newLink.href = url.toString();
            newLink.crossOrigin = "anonymous";
            // until the new link loaded, removing the old one
            document.head.append(newLink);

            // wait for a while until the sheet is fully loaded
            await asyncSleepFor(1500);
            const newSheets = [...document.styleSheets];
            const refreshedSheet = newSheets.find(
              (s) => s.href === newLink.href,
            );
            if (!refreshedSheet) {
              newLink.remove();
              return;
            }
            _jsConsoleLog("parsing recreated the link element: ", newLink.href);
            parseCssSheet(refreshedSheet);
            oldLink.remove();
          } catch (e) {
            _jsConsoleWarn("Error recreating the link element:", e);
            if (newLink) {
              newLink.remove();
            }
          }
        }
      }),
    );
  } catch (e) {
    _jsConsoleError("Error processing stylesheets:", e);
  }

  return hoverMap;
}

// Helper method for debugging
function findNodeById(arr, targetId, path = []) {
  for (let i = 0; i < arr.length; i++) {
    const currentPath = [...path, arr[i].id];
    if (arr[i].id === targetId) {
      _jsConsoleLog("Lineage:", currentPath.join(" -> "));
      return arr[i];
    }
    if (arr[i].children && arr[i].children.length > 0) {
      const result = findNodeById(arr[i].children, targetId, currentPath);
      if (result) {
        return result;
      }
    }
  }
  return null;
}

function getElementDomDepth(elementNode) {
  let depth = 0;
  const rootElement = elementNode.getRootNode().firstElementChild;
  while (elementNode !== rootElement && elementNode.parentElement) {
    depth++;
    elementNode = elementNode.parentElement;
  }
  return depth;
}

if (window.globalOneTimeIncrementElements === undefined) {
  window.globalOneTimeIncrementElements = [];
}

if (window.globalDomDepthMap === undefined) {
  window.globalDomDepthMap = new Map();
}

function isClassNameIncludesHidden(className) {
  // some hidden elements are with the classname like `class="select-items select-hide"` or `class="dropdown-container dropdown-invisible"`
  return (
    className.toLowerCase().includes("hide") ||
    className.toLowerCase().includes("invisible") ||
    className.toLowerCase().includes("closed")
  );
}

function isClassNameIncludesActivatedStatus(className) {
  // some elements are with the classname like `class="open"` or `class="active"` should be considered as activated by the click
  return (
    className.toLowerCase().includes("open") ||
    className.toLowerCase().includes("active")
  );
}

function waitForNextFrame() {
  return new Promise((resolve) => {
    requestAnimationFrame(() => resolve());
  });
}

function asyncSleepFor(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function addIncrementalNodeToMap(parentNode, childrenNode) {
  const maxParsedElement = 3000;
  const maxElementToWait = 100;
  if ((await window.globalParsedElementCounter.get()) > maxParsedElement) {
    _jsConsoleWarn(
      "Too many elements parsed, stopping the observer to parse the elements",
    );
    await window.globalParsedElementCounter.add();
    return;
  }

  // make the dom parser async
  await waitForNextFrame();
  if (window.globalListnerFlag) {
    // calculate the depth of targetNode element for sorting
    const depth = getElementDomDepth(parentNode);
    let newNodesTreeList = [];
    if (window.globalDomDepthMap.has(depth)) {
      newNodesTreeList = window.globalDomDepthMap.get(depth);
    }

    try {
      for (const child of childrenNode) {
        // sleep for a while until animation ends
        if (
          (await window.globalParsedElementCounter.get()) < maxElementToWait
        ) {
          await asyncSleepFor(300);
        }
        // Pass -1 as frame_index to indicate the frame number is not sensitive in this case
        const [_, newNodeTree] = await buildElementTree(
          child,
          "",
          true,
          window.globalHoverStylesMap,
        );
        if (newNodeTree.length > 0) {
          newNodesTreeList.push(...newNodeTree);
        }
      }
    } catch (error) {
      _jsConsoleError("Error building incremental element node:", error);
    }
    window.globalDomDepthMap.set(depth, newNodesTreeList);
  }
  await window.globalParsedElementCounter.add();
}

if (window.globalObserverForDOMIncrement === undefined) {
  window.globalObserverForDOMIncrement = new MutationObserver(async function (
    mutationsList,
    observer,
  ) {
    // TODO: how to detect duplicated recreate element?
    for (const mutation of mutationsList) {
      const node = mutation.target;
      if (node.nodeType === Node.TEXT_NODE) continue;
      const tagName = node.tagName?.toLowerCase();

      // ignore unique_id change to avoid infinite loop about DOM changes
      if (mutation.attributeName === "unique_id") continue;

      // if the changing element is dropdown related elements, we should consider
      // they're the new element as long as the element is still visible on the page
      if (
        isDropdownRelatedElement(node) &&
        getElementComputedStyle(node)?.display !== "none"
      ) {
        window.globalOneTimeIncrementElements.push({
          targetNode: node,
          newNodes: [node],
        });
        await addIncrementalNodeToMap(node, [node]);
        continue;
      }

      // if they're not the dropdown related elements
      // we detect the element based on the following rules
      switch (mutation.type) {
        case "attributes": {
          switch (mutation.attributeName) {
            case "hidden": {
              if (!node.hidden) {
                window.globalOneTimeIncrementElements.push({
                  targetNode: node,
                  newNodes: [node],
                });
                await addIncrementalNodeToMap(node, [node]);
              }
              break;
            }
            case "style": {
              // TODO: need to confirm that elemnent is hidden previously
              if (tagName === "body") continue;
              if (getElementComputedStyle(node)?.display !== "none") {
                window.globalOneTimeIncrementElements.push({
                  targetNode: node,
                  newNodes: [node],
                });
                await addIncrementalNodeToMap(node, [node]);
              }
              break;
            }
            case "class": {
              if (tagName === "body") continue;
              if (!mutation.oldValue) continue;
              const currentClassName = node.className
                ? node.className.toString()
                : "";
              if (
                !isClassNameIncludesHidden(mutation.oldValue) &&
                !isClassNameIncludesActivatedStatus(currentClassName) &&
                !node.hasAttribute("data-menu-uid") && // google framework use this to trace dropdown menu
                !mutation.oldValue.includes("select__items") &&
                !(
                  node.hasAttribute("data-testid") &&
                  node.getAttribute("data-testid").includes("select-dropdown")
                )
              )
                continue;
              if (getElementComputedStyle(node)?.display !== "none") {
                window.globalOneTimeIncrementElements.push({
                  targetNode: node,
                  newNodes: [node],
                });
                await addIncrementalNodeToMap(node, [node]);
              }
              break;
            }
          }
          break;
        }
        case "childList": {
          let changedNode = {
            targetNode: node, // TODO: for future usage, when we want to parse new elements into a tree
          };
          let newNodes = [];
          if (mutation.addedNodes && mutation.addedNodes.length > 0) {
            for (const node of mutation.addedNodes) {
              // skip the text nodes, they won't be interactable
              if (node.nodeType === Node.TEXT_NODE) continue;
              newNodes.push(node);
            }
          }
          if (
            newNodes.length == 0 &&
            (tagName === "ul" ||
              (tagName === "div" &&
                node.hasAttribute("role") &&
                node.getAttribute("role").toLowerCase() === "listbox"))
          ) {
            newNodes.push(node);
          }

          if (newNodes.length > 0) {
            changedNode.newNodes = newNodes;
            window.globalOneTimeIncrementElements.push(changedNode);
            await addIncrementalNodeToMap(
              changedNode.targetNode,
              changedNode.newNodes,
            );
          }
          break;
        }
      }
    }
  });
}

async function startGlobalIncrementalObserver(element = null) {
  window.globalListnerFlag = true;
  window.globalDomDepthMap = new Map();
  window.globalOneTimeIncrementElements = [];
  window.globalHoverStylesMap = await getHoverStylesMap();
  window.globalParsedElementCounter = new SafeCounter();
  window.globalObserverForDOMIncrement.takeRecords(); // cleanup the older data
  window.globalObserverForDOMIncrement.observe(document.body, {
    attributes: true,
    attributeOldValue: true,
    childList: true,
    subtree: true,
    characterData: true,
  });

  // if the element is in shadow DOM, we need to observe the shadow DOM as well
  if (element && element.getRootNode() instanceof ShadowRoot) {
    window.globalObserverForDOMIncrement.observe(element.getRootNode(), {
      attributes: true,
      attributeOldValue: true,
      childList: true,
      subtree: true,
      characterData: true,
    });
  }
}

async function stopGlobalIncrementalObserver() {
  window.globalListnerFlag = false;
  window.globalObserverForDOMIncrement.disconnect();
  window.globalObserverForDOMIncrement.takeRecords(); // cleanup the older data
  while (
    window.globalParsedElementCounter &&
    window.globalOneTimeIncrementElements &&
    (await window.globalParsedElementCounter.get()) <
      window.globalOneTimeIncrementElements.length
  ) {
    await asyncSleepFor(100);
  }
  window.globalOneTimeIncrementElements = [];
  window.globalDomDepthMap = new Map();
}

async function getIncrementElements(wait_until_finished = true) {
  if (wait_until_finished) {
    while (
      (await window.globalParsedElementCounter.get()) <
      window.globalOneTimeIncrementElements.length
    ) {
      await asyncSleepFor(100);
    }
  }

  // cleanup the children tree, remove the duplicated element
  // search starting from the shallowest node:
  // 1. if deeper, the node could only be the children of the shallower one or no related one.
  // 2. if depth is same, the node could only be duplicated one or no related one.
  const idToElement = new Map();
  const cleanedTreeList = [];
  const sortedDepth = Array.from(window.globalDomDepthMap.keys()).sort(
    (a, b) => a - b,
  );
  for (let idx = 0; idx < sortedDepth.length; idx++) {
    const depth = sortedDepth[idx];
    const treeList = window.globalDomDepthMap.get(depth);

    const removeDupAndConcatChildren = async (element) => {
      let children = element.children;
      for (let i = 0; i < children.length; i++) {
        const child = children[i];
        // FIXME: skip to update the element if it is in shadow DOM, since document.querySelector will not work
        if (child.shadowHost) {
          continue;
        }
        const domElement = document.querySelector(`[unique_id="${child.id}"]`);
        // if the element is still on the page, we rebuild the element to update the information
        if (domElement) {
          let newChild = await buildElementObject(
            "",
            domElement,
            child.interactable,
            child.purgeable,
          );
          newChild.children = child.children;
          children[i] = newChild;
        } else {
          children[i].interactable = false;
        }
      }

      if (idToElement.has(element.id)) {
        element = idToElement.get(element.id);
        for (let i = 0; i < children.length; i++) {
          const child = children[i];
          if (!idToElement.get(child.id)) {
            element.children.push(child);
          }
        }
      }
      idToElement.set(element.id, element);
      for (let i = 0; i < children.length; i++) {
        const child = children[i];
        await removeDupAndConcatChildren(child);
      }
    };

    for (let treeHeadElement of treeList) {
      // FIXME: skip to update the element if it is in shadow DOM, since document.querySelector will not work
      if (!treeHeadElement.shadowHost) {
        const domElement = document.querySelector(
          `[unique_id="${treeHeadElement.id}"]`,
        );
        // if the element is still on the page, we rebuild the element to update the information
        if (domElement) {
          let newHead = await buildElementObject(
            "",
            domElement,
            treeHeadElement.interactable,
            treeHeadElement.purgeable,
          );
          newHead.children = treeHeadElement.children;
          treeHeadElement = newHead;
        } else {
          treeHeadElement.interactable = false;
        }
      }

      // check if the element is existed
      if (!idToElement.has(treeHeadElement.id)) {
        cleanedTreeList.push(treeHeadElement);
      }
      await removeDupAndConcatChildren(treeHeadElement);
    }
  }

  return [Array.from(idToElement.values()), cleanedTreeList];
}

function isAnimationFinished() {
  const animations = document.getAnimations({ subtree: true });
  const unfinishedAnimations = animations.filter(
    (a) => a.playState !== "finished",
  );
  if (!unfinishedAnimations || unfinishedAnimations.length == 0) {
    return true;
  }
  return false;
}

/**
 * Remove unique_id attribute from all elements on the page.
 * This includes elements in the main document and shadow DOM.
 */
function removeAllUniqueIds() {
  // Function to recursively remove unique_id from an element and its children
  const removeUniqueIdFromElement = (element) => {
    if (element.hasAttribute("unique_id")) {
      element.removeAttribute("unique_id");
    }

    // Process children in the main DOM
    for (const child of Array.from(element.children)) {
      removeUniqueIdFromElement(child);
    }

    // Process elements in shadow DOM if present
    if (element.shadowRoot) {
      for (const shadowChild of Array.from(element.shadowRoot.children)) {
        removeUniqueIdFromElement(shadowChild);
      }
    }
  };

  // Start from document.documentElement to process the entire page
  removeUniqueIdFromElement(document.documentElement);
}

/**

// How to run the code:

// Get all interactable elements and draw boxes
buildElementsAndDrawBoundingBoxes();

// Remove the boxes
removeBoundingBoxes();

// Get the element tree
const [elements, tree] = buildTreeFromBody();
_jsConsoleLog(elements); // All elements
_jsConsoleLog(tree);     // Tree structure

// Test if a specific element is interactable
const element = document.querySelector('button');
const hoverMap = getHoverStylesMap();
_jsConsoleLog(isInteractable(element, hoverMap));
 */
