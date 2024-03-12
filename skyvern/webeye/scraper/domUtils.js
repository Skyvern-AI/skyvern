// Commands for manipulating rects.
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
  return true;
}

// from playwright
function isElementVisible(element) {
  // TODO: This is a hack to not check visibility for option elements
  // because they are not visible by default. We check their parent instead for visibility.
  if (element.tagName.toLowerCase() === "option")
    return element.parentElement && isElementVisible(element.parentElement);

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
  return rect.width > 0 && rect.height > 0;
}

function isHiddenOrDisabled(element) {
  const style = getElementComputedStyle(element);
  return style?.display === "none" || element.hidden || element.disabled;
}

function isScriptOrStyle(element) {
  const tagName = element.tagName.toLowerCase();
  return tagName === "script" || tagName === "style";
}

function hasWidgetRole(element) {
  const role = element.getAttribute("role");
  if (!role) {
    return false;
  }
  // https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Roles#2._widget_roles
  // Not all roles make sense for the time being so we only check for the ones that do
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
    "textbox",
    "searchbox",
    "slider",
    "spinbutton",
    "switch",
    "gridcell",
  ];
  return widgetRoles.includes(role.toLowerCase().trim());
}

function isInteractableInput(element) {
  const tagName = element.tagName.toLowerCase();
  const type = element.getAttribute("type");
  if (tagName !== "input" || !type) {
    // let other checks decide
    return false;
  }
  const clickableTypes = [
    "button",
    "checkbox",
    "date",
    "datetime-local",
    "email",
    "file",
    "image",
    "month",
    "number",
    "password",
    "radio",
    "range",
    "reset",
    "search",
    "submit",
    "tel",
    "text",
    "time",
    "url",
    "week",
  ];
  return clickableTypes.includes(type.toLowerCase().trim());
}

function isInteractable(element) {
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

  if (tagName === "a" && element.href) {
    return true;
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

  if (tagName === "div" || tagName === "img" || tagName === "span") {
    const computedStyle = window.getComputedStyle(element);
    const hasPointer = computedStyle.cursor === "pointer";
    const hasCursor = computedStyle.cursor === "cursor";
    return hasPointer || hasCursor;
  }

  // support listbox and options underneath it
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

  return false;
}

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

function getElementContext(element) {
  // dfs to collect the non unique_id context
  let fullContext = "";
  if (element.childNodes.length === 0) {
    return fullContext;
  }
  let childContextList = new Array();
  for (var child of element.childNodes) {
    let childContext = "";
    if (child.nodeType === Node.TEXT_NODE) {
      if (!element.hasAttribute("unique_id")) {
        childContext = child.data.trim();
      }
    } else if (child.nodeType === Node.ELEMENT_NODE) {
      if (!child.hasAttribute("unique_id")) {
        childContext = getElementContext(child);
      }
    }
    if (childContext.length > 0) {
      childContextList.push(childContext);
    }

    if (childContextList.length > 0) {
      fullContext = childContextList.join(";");
    }

    const charLimit = 1000;
    if (fullContext.length > charLimit) {
      fullContext = "";
    }
  }
  return fullContext;
}

function getElementContent(element) {
  // DFS to get all the text content from all the nodes under the element

  let textContent = element.textContent;
  let nodeContent = "";
  // if element has children, then build a list of text and join with a semicolon
  if (element.childNodes.length > 0) {
    let childTextContentList = new Array();
    let nodeTextContentList = new Array();
    for (var child of element.childNodes) {
      let childText = "";
      if (child.nodeType === Node.TEXT_NODE) {
        childText = child.data.trim();
        nodeTextContentList.push(childText);
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        // childText = child.textContent.trim();
        childText = getElementContent(child);
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
  const charLimit = 1000;
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
  return selectOptions;
}

function getListboxOptions(element) {
  // get all the elements with role="option" under the element
  var optionElements = element.querySelectorAll('[role="option"]');
  let selectOptions = [];
  for (var i = 0; i < optionElements.length; i++) {
    var ele = optionElements[i];
    selectOptions.push({
      optionIndex: i,
      text: removeMultipleSpaces(ele.textContent),
    });
  }
  return selectOptions;
}

function buildTreeFromBody() {
  var elements = [];
  var resultArray = [];

  function buildElementObject(element) {
    var element_id = elements.length;
    var elementTagNameLower = element.tagName.toLowerCase();
    element.setAttribute("unique_id", element_id);
    // if element is an "a" tag and has a target="_blank" attribute, remove the target attribute
    // We're doing this so that skyvern can do all the navigation in a single page/tab and not open new tab
    if (element.tagName.toLowerCase() === "a") {
      if (element.getAttribute("target") === "_blank") {
        element.removeAttribute("target");
      }
    }
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
        attr.name === "aria-readonly"
      ) {
        attrValue = true;
      }
      attrs[attr.name] = attrValue;
    }
    if (elementTagNameLower === "input" || elementTagNameLower === "textarea") {
      attrs["value"] = element.value;
    }

    let elementObj = {
      id: element_id,
      tagName: elementTagNameLower,
      attributes: attrs,
      text: getElementContent(element),
      children: [],
      rect: DomUtils.getVisibleClientRect(element, true),
    };

    // get options for select element or for listbox element
    let selectOptions = null;
    if (elementTagNameLower === "select") {
      selectOptions = getSelectOptions(element);
    } else if (attrs["role"] && attrs["role"].toLowerCase() === "listbox") {
      // if "role" key is inside attrs, then get all the elements with role "option" and get their text
      selectOptions = getListboxOptions(element);
    }
    if (selectOptions) {
      elementObj.options = selectOptions;
    }

    return elementObj;
  }

  function getChildElements(element) {
    if (element.childElementCount !== 0) {
      return Array.from(element.children);
    } else {
      return [];
    }
  }
  function processElement(element, interactableParentId) {
    // Check if the element is interactable
    if (isInteractable(element)) {
      var elementObj = buildElementObject(element);
      elements.push(elementObj);
      // If the element is interactable but has no interactable parent,
      // then it starts a new tree, so add it to the result array
      // and set its id as the interactable parent id for the next elements
      // under it
      if (interactableParentId === null) {
        resultArray.push(elementObj);
      }
      // If the element is interactable and has an interactable parent,
      // then add it to the children of the parent
      else {
        elements[interactableParentId].children.push(elementObj);
      }
      // Recursively process the children of the element
      getChildElements(element).forEach((child) => {
        processElement(child, elementObj.id);
      });
      return elementObj;
    } else {
      // For a non-interactable element, process its children
      // and check if any of them are interactable
      let interactableChildren = [];
      getChildElements(element).forEach((child) => {
        let children = processElement(child, interactableParentId);
      });
    }
  }

  // TODO: Handle iframes

  // Clear all the unique_id attributes so that there are no conflicts
  removeAllUniqueIdAttributes();
  processElement(document.body, null);

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

    // for most elements, we're going 10 layers up to see if we can find "label" as a parent
    // if found, most likely the context under label is relevant to this element
    let targetParentElements = new Set(["label", "fieldset"]);

    // look up for 10 levels to find the most contextual parent element
    let targetContextualParent = null;
    let currentEle = document.querySelector(`[unique_id="${element.id}"]`);
    let parentEle = currentEle;
    for (var i = 0; i < 10; i++) {
      parentEle = parentEle.parentElement;
      if (parentEle) {
        if (targetParentElements.has(parentEle.tagName.toLowerCase())) {
          targetContextualParent = parentEle;
        }
      } else {
        break;
      }
    }
    if (targetContextualParent) {
      let context = "";
      var lowerCaseTagName = targetContextualParent.tagName.toLowerCase();
      if (lowerCaseTagName === "label") {
        context = getElementContext(targetContextualParent);
      } else if (lowerCaseTagName === "fieldset") {
        // fieldset is usually within a form or another element that contains the whole context
        targetContextualParent = targetContextualParent.parentElement;
        if (targetContextualParent) {
          context = getElementContext(targetContextualParent);
        }
      }
      if (context.length > 0) {
        element.context = context;
      }
    }
  }

  return [elements, resultArray];
}

function drawBoundingBoxes(elements) {
  // draw a red border around the elements
  var groups = groupElementsVisually(elements);
  var hintMarkers = createHintMarkersForGroups(groups);
  addHintMarkersToPage(hintMarkers);
}

function removeAllUniqueIdAttributes() {
  var elementsWithUniqueId = document.querySelectorAll("[unique_id]");

  elementsWithUniqueId.forEach(function (element) {
    element.removeAttribute("unique_id");
  });
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

  const hintMarkers = groups.map((group) => createHintMarkerForGroup(group));

  // fill in marker text
  const hintStrings = generateHintStrings(hintMarkers.length);
  for (let i = 0; i < hintMarkers.length; i++) {
    const hintMarker = hintMarkers[i];
    hintMarker.hintString = hintStrings[i];
    hintMarker.element.innerHTML = hintMarker.hintString.toUpperCase();
  }

  return hintMarkers;
}

function createHintMarkerForGroup(group) {
  const marker = {};
  // yellow annotation box with string
  const el = document.createElement("div");
  el.style.left = group.rect.left + "px";
  el.style.top = group.rect.top + "px";
  // Each group is assigned a different incremental z-index, we use the same z-index for the
  // bounding box and the hint marker
  el.style.zIndex = this.currentZIndex;

  // The bounding box around the group of hints.
  const boundingBox = document.createElement("div");

  // Calculate the position of the element relative to the document
  var scrollTop = window.pageYOffset || document.documentElement.scrollTop;
  var scrollLeft = window.pageXOffset || document.documentElement.scrollLeft;

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
    // parent.appendChild(hintMarker.element);
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
  window.scrollTo(0, 0);
  scrollDownAndUp();
  if (draw_boxes) {
    var elementsAndResultArray = buildTreeFromBody();
    drawBoundingBoxes(elementsAndResultArray[0]);
  }
  return window.scrollY;
}

function scrollToNextPage(draw_boxes) {
  // remove bounding boxes, scroll to next page with 200px overlap, then draw bounding boxes again
  // return true if there is a next page, false otherwise
  removeBoundingBoxes();
  window.scrollBy(0, window.innerHeight - 200);
  scrollUpAndDown();
  if (draw_boxes) {
    var elementsAndResultArray = buildTreeFromBody();
    drawBoundingBoxes(elementsAndResultArray[0]);
  }
  return window.scrollY;
}

function scrollUpAndDown() {
  // remove select2-drop-above class to prevent dropdown from being rendered on top of the box
  // then scroll up by 1 and scroll down by 1
  removeSelect2DropAbove();
  window.scrollBy(0, -1);
  removeSelect2DropAbove();
  window.scrollBy(0, 1);
}

function scrollDownAndUp() {
  // remove select2-drop-above class to prevent dropdown from being rendered on top of the box
  // then scroll up by 1 and scroll down by 1
  removeSelect2DropAbove();
  window.scrollBy(0, 1);
  removeSelect2DropAbove();
  window.scrollBy(0, -1);
}

function removeSelect2DropAbove() {
  var select2DropAbove = document.getElementsByClassName("select2-drop-above");
  var allElements = [];
  for (var i = 0; i < select2DropAbove.length; i++) {
    allElements.push(select2DropAbove[i]);
  }
  allElements.forEach((ele) => {
    ele.classList.remove("select2-drop-above");
  });
}
