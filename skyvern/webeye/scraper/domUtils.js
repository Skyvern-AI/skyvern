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

  static getVisibleClientRect(element, testChildren) {
    // Note: this call will be expensive if we modify the DOM in between calls.
    let clientRect;
    if (testChildren == null) testChildren = false;
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
      const elementComputedStyle = window.getComputedStyle(element, null);
      const isInlineZeroFontSize =
        0 ===
          elementComputedStyle.getPropertyValue("display").indexOf("inline") &&
        elementComputedStyle.getPropertyValue("font-size") === "0px";
      // Override the function to return this value for the rest of this context.
      isInlineZeroHeight = () => isInlineZeroFontSize;
      return isInlineZeroFontSize;
    };

    for (clientRect of clientRects) {
      // If the link has zero dimensions, it may be wrapping visible but floated elements. Check for
      // this.
      let computedStyle;
      if ((clientRect.width === 0 || clientRect.height === 0) && testChildren) {
        for (const child of Array.from(element.children)) {
          computedStyle = window.getComputedStyle(child, null);
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
          return childClientRect;
        }
      } else {
        clientRect = this.cropRectToVisible(clientRect);

        if (
          clientRect === null ||
          clientRect.width < 3 ||
          clientRect.height < 3
        )
          continue;

        // eliminate invisible elements (see test_harnesses/visibility_test.html)
        computedStyle = window.getComputedStyle(element, null);
        if (computedStyle.getPropertyValue("visibility") !== "visible")
          continue;

        return clientRect;
      }
    }

    return null;
  }

  static getViewportTopLeft() {
    const box = document.documentElement;
    const style = getComputedStyle(box);
    const rect = box.getBoundingClientRect();
    if (
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

// from playwright
function getElementComputedStyle(element, pseudo) {
  return element.ownerDocument && element.ownerDocument.defaultView
    ? element.ownerDocument.defaultView.getComputedStyle(element, pseudo)
    : undefined;
}

// from playwright
function isElementStyleVisibilityVisible(element, style) {
  style = style ?? getElementComputedStyle(element);
  if (!style) return true;
  if (
    !element.checkVisibility({ checkOpacity: false, checkVisibilityCSS: false })
  )
    return false;
  if (style.visibility !== "visible") return false;

  // TODO: support style.clipPath and style.clipRule?
  // if element is clipped with rect(0px, 0px, 0px, 0px), it means it's invisible on the page
  if (style.clip === "rect(0px, 0px, 0px, 0px)") {
    return false;
  }

  return true;
}

// from playwright
function isElementVisible(element) {
  // TODO: This is a hack to not check visibility for option elements
  // because they are not visible by default. We check their parent instead for visibility.
  if (
    element.tagName.toLowerCase() === "option" ||
    (element.tagName.toLowerCase() === "input" && element.type === "radio")
  )
    return element.parentElement && isElementVisible(element.parentElement);

  const className = element.className.toString();
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
      // skipping other nodes including text
    }
    return false;
  }
  if (!isElementStyleVisibilityVisible(element, style)) return false;
  const rect = element.getBoundingClientRect();
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

function isInteractableInput(element) {
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
  return !isReadonlyElement(element) && type !== "hidden";
}

function isInteractable(element) {
  if (element.shadowRoot) {
    return false;
  }
  if (!isElementVisible(element)) {
    return false;
  }

  if (isHiddenOrDisabled(element)) {
    return false;
  }

  if (isScriptOrStyle(element)) {
    return false;
  }

  if (hasWidgetRole(element)) {
    return true;
  }

  if (isInteractableInput(element)) {
    return true;
  }

  const tagName = element.tagName.toLowerCase();

  if (tagName === "iframe") {
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

  if (tagName === "div" || tagName === "span") {
    if (hasAngularClickBinding(element)) {
      return true;
    }
    // https://www.oxygenxml.com/dita/1.3/specs/langRef/technicalContent/svg-container.html
    // svg-container is usually used for clickable elements that wrap SVGs
    if (element.className.toString().includes("svg-container")) {
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
    tagName === "div" &&
    element.hasAttribute("aria-disabled") &&
    element.getAttribute("aria-disabled").toLowerCase() === "false"
  ) {
    return true;
  }

  if (
    tagName === "div" ||
    tagName === "img" ||
    tagName === "span" ||
    tagName === "a" ||
    tagName === "i" ||
    tagName === "li"
  ) {
    const computedStyle = window.getComputedStyle(element);
    if (computedStyle.cursor === "pointer") {
      return true;
    }
    // FIXME: hardcode to fix the bug about hover style now
    if (element.className.toString().includes("hover:cursor-pointer")) {
      return true;
    }
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
  const style = window.getComputedStyle(element);
  return (
    style.overflow === "auto" ||
    style.overflow === "scroll" ||
    style.overflowX === "auto" ||
    style.overflowX === "scroll" ||
    style.overflowY === "auto" ||
    style.overflowY === "scroll"
  );
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

function hasNgAttribute(element) {
  for (let attr of element.attributes) {
    if (attr.name.startsWith("ng-")) {
      return true;
    }
  }
  return false;
}

const isAngularDropdown = (element) => {
  if (!hasNgAttribute(element)) {
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
  if (!str) {
    return str;
  }
  return str.replace(/\s+/g, " ");
}

function cleanupText(text) {
  return removeMultipleSpaces(
    text.replace("SVGs not supported by this browser.", ""),
  ).trim();
}

const checkStringIncludeRequire = (str) => {
  return (
    str.toLowerCase().includes("*") ||
    str.toLowerCase().includes("✱") ||
    str.toLowerCase().includes("require")
  );
};

const checkRequiredFromStyle = (element) => {
  const afterCustom = getElementComputedStyle(element, "::after")
    .getPropertyValue("content")
    .replace(/"/g, "");
  if (checkStringIncludeRequire(afterCustom)) {
    return true;
  }

  if (!element.className || typeof element.className !== "string") {
    return false;
  }

  return element.className.toLowerCase().includes("require");
};

function getElementContext(element) {
  // dfs to collect the non unique_id context
  let fullContext = new Array();

  // sometimes '*' shows as an after custom style
  const afterCustom = getElementComputedStyle(element, "::after")
    .getPropertyValue("content")
    .replace(/"/g, "");
  if (
    afterCustom.toLowerCase().includes("*") ||
    afterCustom.toLowerCase().includes("require")
  ) {
    fullContext.push(afterCustom);
  }
  if (element.childNodes.length === 0) {
    return fullContext.join(";");
  }
  // if the element already has a context, then add it to the list first
  for (var child of element.childNodes) {
    let childContext = "";
    if (child.nodeType === Node.TEXT_NODE && isElementVisible(element)) {
      if (!element.hasAttribute("unique_id")) {
        childContext = getVisibleText(child).trim();
      }
    } else if (child.nodeType === Node.ELEMENT_NODE) {
      if (!child.hasAttribute("unique_id") && isElementVisible(child)) {
        childContext = getElementContext(child);
      }
    }
    if (childContext.length > 0) {
      fullContext.push(childContext);
    }
  }
  return fullContext.join(";");
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

function getElementContent(element, skipped_element = null) {
  // DFS to get all the text content from all the nodes under the element
  if (skipped_element && element === skipped_element) {
    return "";
  }

  let textContent = getVisibleText(element);
  let nodeContent = "";
  // if element has children, then build a list of text and join with a semicolon
  if (element.childNodes.length > 0) {
    let childTextContentList = new Array();
    let nodeTextContentList = new Array();
    for (var child of element.childNodes) {
      let childText = "";
      if (child.nodeType === Node.TEXT_NODE) {
        childText = getVisibleText(child).trim();
        if (childText.length > 0) {
          nodeTextContentList.push(childText);
        }
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        // childText = child.textContent.trim();
        childText = getElementContent(child, skipped_element);
      } else {
        console.log("Unhandled node type: ", child.nodeType);
      }
      if (childText.length > 0) {
        childTextContentList.push(childText);
      }
    }
    textContent = childTextContentList.join(";");
    nodeContent = cleanupText(nodeTextContentList.join(";"));
  }
  let finalTextContent = cleanupText(textContent);
  // Currently we don't support too much context. Character limit is 1000 per element.
  // we don't think element context has to be that big
  const charLimit = 5000;
  if (finalTextContent.length > charLimit) {
    if (nodeContent.length <= charLimit) {
      finalTextContent = nodeContent;
    } else {
      finalTextContent = "";
    }
  }

  return finalTextContent;
}

function getSelectOptions(element) {
  const options = Array.from(element.options);
  const selectOptions = [];

  for (const option of options) {
    selectOptions.push({
      optionIndex: option.index,
      text: removeMultipleSpaces(option.textContent),
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
      console.log(
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

function uniqueId() {
  const characters =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let result = "";
  for (let i = 0; i < 4; i++) {
    const randomIndex = Math.floor(Math.random() * characters.length);
    result += characters[randomIndex];
  }
  return result;
}

function buildElementObject(frame, element, interactable, purgeable = false) {
  var element_id = element.getAttribute("unique_id") ?? uniqueId();
  var elementTagNameLower = element.tagName.toLowerCase();
  element.setAttribute("unique_id", element_id);

  const attrs = {};
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

  if (
    checkRequiredFromStyle(element) &&
    !attrs["required"] &&
    !attrs["aria-required"]
  ) {
    attrs["required"] = true;
  }

  if (
    elementTagNameLower === "input" &&
    (element.type === "radio" || element.type === "checkbox")
  ) {
    // if checkbox and radio don't have "checked" and "aria-checked", add a checked="false" to help LLM understand
    if (!("checked" in attrs) && !("aria-checked" in attrs)) {
      attrs["checked"] = false;
    }
  }

  if (elementTagNameLower === "input" || elementTagNameLower === "textarea") {
    attrs["value"] = element.value;
  }

  let elementObj = {
    id: element_id,
    frame: frame,
    interactable: interactable,
    tagName: elementTagNameLower,
    attributes: attrs,
    text: getElementContent(element),
    children: [],
    rect: DomUtils.getVisibleClientRect(element, true),
    // if purgeable is True, which means this element is only used for building the tree relationship
    purgeable: purgeable,
    // don't trim any attr of this element if keepAllAttr=True
    keepAllAttr:
      elementTagNameLower === "svg" || element.closest("svg") !== null,
    isSelectable:
      elementTagNameLower === "select" ||
      isDropdownButton(element) ||
      isAngularDropdown(element) ||
      isSelect2Dropdown(element) ||
      isSelect2MultiChoice(element),
  };

  let isInShadowRoot = element.getRootNode() instanceof ShadowRoot;
  if (isInShadowRoot) {
    let shadowHostEle = element.getRootNode().host;
    let shadowHostId = shadowHostEle.getAttribute("unique_id");
    // assign shadowHostId to the shadowHost element if it doesn't have unique_id
    if (!shadowHostId) {
      shadowHostId = uniqueId();
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

function buildTreeFromBody(frame = "main.frame") {
  return buildElementTree(document.body, frame);
}

function buildElementTree(starter = document.body, frame, full_tree = false) {
  var elements = [];
  var resultArray = [];

  function getChildElements(element) {
    if (element.childElementCount !== 0) {
      return Array.from(element.children);
    } else {
      return [];
    }
  }
  function processElement(element, parentId) {
    if (element === null) {
      console.log("get a null element");
      return;
    }

    // skip proccessing option element as they are already added to the select.options
    if (element.tagName.toLowerCase() === "option") {
      return;
    }

    // if element is an "a" tag and has a target="_blank" attribute, remove the target attribute
    // We're doing this so that skyvern can do all the navigation in a single page/tab and not open new tab
    if (element.tagName.toLowerCase() === "a") {
      if (element.getAttribute("target") === "_blank") {
        element.removeAttribute("target");
      }
    }

    // Check if the element is interactable
    if (isInteractable(element)) {
      var elementObj = buildElementObject(frame, element, true);
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
      // Recursively process the children of the element
      const children = getChildElements(element);
      for (let i = 0; i < children.length; i++) {
        const childElement = children[i];
        processElement(childElement, elementObj.id);
      }
      return elementObj;
    } else if (element.tagName.toLowerCase() === "iframe") {
      let iframeElementObject = buildElementObject(frame, element, false);

      elements.push(iframeElementObject);
      resultArray.push(iframeElementObject);
    } else if (element.shadowRoot) {
      // shadow host element
      let shadowHostElement = buildElementObject(frame, element, false);
      elements.push(shadowHostElement);
      resultArray.push(shadowHostElement);

      const children = getChildElements(element.shadowRoot);
      for (let i = 0; i < children.length; i++) {
        const childElement = children[i];
        processElement(childElement, shadowHostElement.id);
      }
    } else {
      // For a non-interactable element, if it has direct text, we also tagged
      // it with unique_id, but with interatable=false in the element.
      // After that, process its children
      // and check if any of them are interactable
      let interactableChildren = [];
      if (
        isElementVisible(element) &&
        !isHidden(element) &&
        !isScriptOrStyle(element)
      ) {
        let elementObj = null;
        let isParentSVG = element.closest("svg");
        if (element.tagName.toLowerCase() === "svg") {
          // if element is <svg> we save all attributes and its children
          elementObj = buildElementObject(frame, element, false);
        } else if (isParentSVG && isParentSVG.getAttribute("unique_id")) {
          // if elemnet is the children of the <svg> with an unique_id
          elementObj = buildElementObject(frame, element, false);
        } else if (isTableRelatedElement(element)) {
          // build all table related elements into skyvern element
          // we need these elements to preserve the DOM structure
          elementObj = buildElementObject(frame, element, false);
        } else if (full_tree) {
          // when building full tree, we only get text from element itself
          // elements without text are purgeable
          elementObj = buildElementObject(frame, element, false, true);
          let textContent = "";
          if (isElementVisible(element)) {
            for (let i = 0; i < element.childNodes.length; i++) {
              var node = element.childNodes[i];
              if (node.nodeType === Node.TEXT_NODE) {
                textContent += node.data.trim();
              }
            }
          }
          elementObj.text = textContent;
          if (textContent.length > 0) {
            elementObj.purgeable = false;
          }
        } else {
          // character length limit for non-interactable elements should be 5000
          // we don't use element context in HTML format,
          // so we need to make sure we parse all text node to avoid missing text in HTML.
          let textContent = "";
          for (let i = 0; i < element.childNodes.length; i++) {
            var node = element.childNodes[i];
            if (node.nodeType === Node.TEXT_NODE) {
              textContent += getVisibleText(node).trim();
            }
          }
          if (textContent && textContent.length <= 5000) {
            elementObj = buildElementObject(frame, element, false);
          }
        }

        if (elementObj !== null) {
          elements.push(elementObj);
          if (parentId === null) {
            resultArray.push(elementObj);
          } else {
            // TODO: use dict/object so that we access these in O(1) instead
            elements
              .find((element) => element.id === parentId)
              .children.push(elementObj);
          }
          parentId = elementObj.id;
        }
      }

      const children = getChildElements(element);
      for (let i = 0; i < children.length; i++) {
        const childElement = children[i];
        processElement(childElement, parentId);
      }
    }
  }

  const getContextByParent = (element, ctx) => {
    // for most elements, we're going 10 layers up to see if we can find "label" as a parent
    // if found, most likely the context under label is relevant to this element
    let targetParentElements = new Set(["label", "fieldset"]);

    // look up for 10 levels to find the most contextual parent element
    let targetContextualParent = null;
    let currentEle = getDOMElementBySkyvenElement(element);
    if (!currentEle) {
      return ctx;
    }
    let parentEle = currentEle;
    for (var i = 0; i < 10; i++) {
      parentEle = parentEle.parentElement;
      if (parentEle) {
        if (
          targetParentElements.has(parentEle.tagName.toLowerCase()) ||
          (typeof parentEle.className === "string" &&
            checkParentClass(parentEle.className.toLowerCase()))
        ) {
          targetContextualParent = parentEle;
        }
      } else {
        break;
      }
    }
    if (!targetContextualParent) {
      return ctx;
    }

    let context = "";
    var lowerCaseTagName = targetContextualParent.tagName.toLowerCase();
    if (lowerCaseTagName === "fieldset") {
      // fieldset is usually within a form or another element that contains the whole context
      targetContextualParent = targetContextualParent.parentElement;
      if (targetContextualParent) {
        context = getElementContext(targetContextualParent);
      }
    } else {
      context = getElementContext(targetContextualParent);
    }
    if (context.length > 0) {
      ctx.push(context);
    }
    return ctx;
  };

  const getContextByLinked = (element, ctx) => {
    let currentEle = getDOMElementBySkyvenElement(element);
    if (!currentEle) {
      return ctx;
    }

    const document = currentEle.getRootNode();
    // check labels pointed to this element
    // 1. element id -> labels pointed to this id
    // 2. by attr "aria-labelledby" -> only one label with this id
    let linkedElements = new Array();
    const elementId = currentEle.getAttribute("id");
    if (elementId) {
      try {
        linkedElements = [
          ...document.querySelectorAll(`label[for="${elementId}"]`),
        ];
      } catch (e) {
        console.log("failed to query labels: ", e);
      }
    }
    const labelled = currentEle.getAttribute("aria-labelledby");
    if (labelled) {
      const label = document.getElementById(labelled);
      if (label) {
        linkedElements.push(label);
      }
    }
    const described = currentEle.getAttribute("aria-describedby");
    if (described) {
      const describe = document.getElementById(described);
      if (describe) {
        linkedElements.push(describe);
      }
    }

    const fullContext = new Array();
    for (let i = 0; i < linkedElements.length; i++) {
      const linked = linkedElements[i];
      // if the element is a child of the label, we should stop to get context before the element
      const content = getElementContent(linked, currentEle);
      if (content) {
        fullContext.push(content);
      }
    }

    const context = fullContext.join(";");
    if (context.length > 0) {
      ctx.push(context);
    }
    return ctx;
  };

  const getContextByTable = (element, ctx) => {
    // pass element's parent's context to the element for listed tags
    let tagsWithDirectParentContext = new Set(["a"]);
    // if the element is a child of a td, th, or tr, then pass the grandparent's context to the element
    let parentTagsThatDelegateParentContext = new Set(["td", "th", "tr"]);
    if (tagsWithDirectParentContext.has(element.tagName)) {
      let curElement = getDOMElementBySkyvenElement(element);
      if (!curElement) {
        return ctx;
      }
      let parentElement = curElement.parentElement;
      if (!parentElement) {
        return ctx;
      }
      if (
        parentTagsThatDelegateParentContext.has(
          parentElement.tagName.toLowerCase(),
        )
      ) {
        let grandParentElement = parentElement.parentElement;
        if (grandParentElement) {
          let context = getElementContext(grandParentElement);
          if (context.length > 0) {
            ctx.push(context);
          }
        }
      }
      let context = getElementContext(parentElement);
      if (context.length > 0) {
        ctx.push(context);
      }
    }
    return ctx;
  };

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

  const trimDuplicatedContext = (element) => {
    if (element.children.length === 0) {
      return;
    }

    // DFS to delete duplicated context
    element.children.forEach((child) => {
      trimDuplicatedContext(child);
      if (element.context === child.context) {
        delete child.context;
      }
      if (child.context) {
        child.context = child.context.replace(element.text, "");
        if (!child.context) {
          delete child.context;
        }
      }
    });
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
          !labelElement.getAttribute("for")
        ) {
          continue;
        }
      }
      trimmedResults.push(element);
    }
    return trimmedResults;
  };

  // setup before parsing the dom
  processElement(starter, null);

  for (var element of elements) {
    if (
      ((element.tagName === "input" && element.attributes["type"] === "text") ||
        element.tagName === "textarea") &&
      (element.attributes["required"] || element.attributes["aria-required"]) &&
      element.attributes.value === ""
    ) {
      // TODO (kerem): we may want to pass these elements to the LLM as empty but required fields in the future
      console.log(
        "input element with required attribute and no value",
        element,
      );
    }

    let ctxList = [];
    try {
      ctxList = getContextByLinked(element, ctxList);
    } catch (e) {
      console.error("failed to get context by linked: ", e);
    }

    try {
      ctxList = getContextByParent(element, ctxList);
    } catch (e) {
      console.error("failed to get context by parent: ", e);
    }

    try {
      ctxList = getContextByTable(element, ctxList);
    } catch (e) {
      console.error("failed to get context by table: ", e);
    }
    const context = ctxList.join(";");
    if (context && context.length <= 5000) {
      element.context = context;
    }

    // FIXME: skip <a> for now to prevent navigating to other page by mistake
    if (element.tagName !== "a" && checkStringIncludeRequire(context)) {
      if (
        !element.attributes["required"] &&
        !element.attributes["aria-required"]
      ) {
        element.attributes["required"] = true;
      }
    }
  }

  resultArray = removeOrphanNode(resultArray);
  resultArray.forEach((root) => {
    trimDuplicatedText(root);
    trimDuplicatedContext(root);
  });

  return [elements, resultArray];
}

function drawBoundingBoxes(elements) {
  // draw a red border around the elements
  var groups = groupElementsVisually(elements);
  var hintMarkers = createHintMarkersForGroups(groups);
  addHintMarkersToPage(hintMarkers);
}

function buildElementsAndDrawBoundingBoxes() {
  var elementsAndResultArray = buildTreeFromBody();
  drawBoundingBoxes(elementsAndResultArray[0]);
}

function captchaSolvedCallback() {
  console.log("captcha solved");
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
  const groups = [];
  // o n^2
  // go through each hint and see if it overlaps with any other hints, if it does, add it to the group of the other hint
  // *** if we start from the bigger elements (top -> bottom) we can avoid merging groups
  for (const element of elements) {
    if (!element.rect) {
      continue;
    }
    const group = groups.find((group) => {
      for (const groupElement of group.elements) {
        if (Rect.intersects(groupElement.rect, element.rect)) {
          return true;
        }
      }
      return false;
    });
    if (group) {
      group.elements.push(element);
    } else {
      groups.push({
        elements: [element],
      });
    }
  }

  // go through each group and create a rectangle that encompasses all the hints in the group
  for (const group of groups) {
    group.rect = createRectangleForGroup(group);
  }

  return groups;
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
    console.log("No groups found, not adding hint markers to page.");
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
        const escapeHTMLPolicy = trustedTypes.createPolicy("default", {
          createHTML: (string) => string,
        });
        hintMarker.element.innerHTML = escapeHTMLPolicy.createHTML(
          hintMarker.hintString.toUpperCase(),
        );
      } else {
        console.error("trustedTypes is not supported in this environment.");
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

function scrollToTop(draw_boxes) {
  removeBoundingBoxes();
  window.scroll({ left: 0, top: 0, behavior: "instant" });
  if (draw_boxes) {
    buildElementsAndDrawBoundingBoxes();
  }
  return window.scrollY;
}

function getScrollXY() {
  return [window.scrollX, window.scrollY];
}

function scrollToXY(x, y) {
  window.scroll({ left: x, top: y, behavior: "instant" });
}

function scrollToNextPage(draw_boxes) {
  // remove bounding boxes, scroll to next page with 200px overlap, then draw bounding boxes again
  // return true if there is a next page, false otherwise
  removeBoundingBoxes();
  window.scrollBy({
    left: 0,
    top: window.innerHeight - 200,
    behavior: "instant",
  });
  if (draw_boxes) {
    buildElementsAndDrawBoundingBoxes();
  }
  return window.scrollY;
}

function isWindowScrollable() {
  // Check if the body's overflow style is set to hidden
  const bodyOverflow = window.getComputedStyle(document.body).overflow;
  const htmlOverflow = window.getComputedStyle(
    document.documentElement,
  ).overflow;

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

// Helper method for debugging
function findNodeById(arr, targetId, path = []) {
  for (let i = 0; i < arr.length; i++) {
    const currentPath = [...path, arr[i].id];
    if (arr[i].id === targetId) {
      console.log("Lineage:", currentPath.join(" -> "));
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
  // some hidden elements are with the classname like `class="select-items select-hide"`
  return className.toLowerCase().includes("hide");
}

function addIncrementalNodeToMap(parentNode, childrenNode) {
  // calculate the depth of targetNode element for sorting
  const depth = getElementDomDepth(parentNode);
  let newNodesTreeList = [];
  if (window.globalDomDepthMap.has(depth)) {
    newNodesTreeList = window.globalDomDepthMap.get(depth);
  }

  for (const child of childrenNode) {
    const [_, newNodeTree] = buildElementTree(child, "", true);
    if (newNodeTree.length > 0) {
      newNodesTreeList.push(...newNodeTree);
    }
  }
  window.globalDomDepthMap.set(depth, newNodesTreeList);
}

if (window.globalObserverForDOMIncrement === undefined) {
  window.globalObserverForDOMIncrement = new MutationObserver(function (
    mutationsList,
    observer,
  ) {
    // TODO: how to detect duplicated recreate element?
    for (const mutation of mutationsList) {
      if (mutation.type === "attributes") {
        if (mutation.attributeName === "hidden") {
          const node = mutation.target;
          if (!node.hidden) {
            window.globalOneTimeIncrementElements.push({
              targetNode: node,
              newNodes: [node],
            });
            addIncrementalNodeToMap(node, [node]);
          }
        }
        if (mutation.attributeName === "style") {
          // TODO: need to confirm that elemnent is hidden previously
          const node = mutation.target;
          if (node.nodeType === Node.TEXT_NODE) continue;
          const newStyle = window.getComputedStyle(node);
          const newDisplay = newStyle.display;
          if (newDisplay !== "none") {
            window.globalOneTimeIncrementElements.push({
              targetNode: node,
              newNodes: [node],
            });
            addIncrementalNodeToMap(node, [node]);
          }
        }
        if (mutation.attributeName === "class") {
          const node = mutation.target;
          if (
            !mutation.oldValue ||
            !isClassNameIncludesHidden(mutation.oldValue)
          )
            continue;
          const newStyle = window.getComputedStyle(node);
          const newDisplay = newStyle.display;
          if (newDisplay !== "none") {
            window.globalOneTimeIncrementElements.push({
              targetNode: node,
              newNodes: [node],
            });
            addIncrementalNodeToMap(node, [node]);
          }
        }
      }

      if (mutation.type === "childList") {
        let changedNode = {
          targetNode: mutation.target, // TODO: for future usage, when we want to parse new elements into a tree
        };
        let newNodes = [];
        if (mutation.addedNodes && mutation.addedNodes.length > 0) {
          for (const node of mutation.addedNodes) {
            // skip the text nodes, they won't be interactable
            if (node.nodeType === Node.TEXT_NODE) continue;
            newNodes.push(node);
          }
        }
        if (newNodes.length > 0) {
          changedNode.newNodes = newNodes;
          window.globalOneTimeIncrementElements.push(changedNode);
          addIncrementalNodeToMap(changedNode.targetNode, changedNode.newNodes);
        }
      }
    }
  });
}

function startGlobalIncrementalObserver() {
  window.globalDomDepthMap = new Map();
  window.globalOneTimeIncrementElements = [];
  window.globalObserverForDOMIncrement.takeRecords(); // cleanup the older data
  window.globalObserverForDOMIncrement.observe(document.body, {
    attributes: true,
    attributeOldValue: true,
    childList: true,
    subtree: true,
    characterData: true,
  });
}

function stopGlobalIncrementalObserver() {
  window.globalDomDepthMap = new Map();
  window.globalObserverForDOMIncrement.disconnect();
  window.globalObserverForDOMIncrement.takeRecords(); // cleanup the older data
  window.globalOneTimeIncrementElements = [];
}

function getIncrementElements() {
  // cleanup the chidren tree, remove the duplicated element
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

    const removeDupAndConcatChildren = (element) => {
      let children = element.children;
      for (let i = 0; i < children.length; i++) {
        const child = children[i];
        const domElement = document.querySelector(`[unique_id="${child.id}"]`);
        // if the element is still on the page, we rebuild the element to update the information
        if (domElement) {
          let newChild = buildElementObject(
            "",
            domElement,
            child.interactable,
            child.purgeable,
          );
          newChild.children = child.children;
          children[i] = newChild;
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
        removeDupAndConcatChildren(child);
      }
    };

    for (let treeHeadElement of treeList) {
      const domElement = document.querySelector(
        `[unique_id="${treeHeadElement.id}"]`,
      );
      // if the element is still on the page, we rebuild the element to update the information
      if (domElement) {
        let newHead = buildElementObject(
          "",
          domElement,
          treeHeadElement.interactable,
          treeHeadElement.purgeable,
        );
        newHead.children = treeHeadElement.children;
        treeHeadElement = newHead;
      }

      // check if the element is existed
      if (!idToElement.has(treeHeadElement.id)) {
        cleanedTreeList.push(treeHeadElement);
      }
      removeDupAndConcatChildren(treeHeadElement);
    }
  }

  return [Array.from(idToElement.values()), cleanedTreeList];
}
