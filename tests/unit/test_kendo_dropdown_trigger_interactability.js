/**
 * Regression tests for Kendo UI DropDownList value triggers in domUtils.js.
 *
 * A Kendo DropDownList renders its selected value as <span class="k-input-value-text">
 * inside a picker whose open/close is delegated on the widget wrapper. When the wrapper
 * is not surfaced (no combobox role, no button toggle, default cursor), the value span is
 * the only text-bearing node the agent can aim at, yet the other checks leave it
 * non-interactable, so the dropdown can never be opened. isInteractable must surface the
 * value span, while ordinary spans and substring-only class tokens stay non-interactable.
 *
 * Exit 0 = pass, exit 1 = failures on stderr.
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(
  path.join(__dirname, "../../skyvern/webeye/scraper/domUtils.js"),
  "utf8",
);

function getComputedStyle(element) {
  return {
    display: "block",
    visibility: "visible",
    opacity: "1",
    cursor: (element._style && element._style.cursor) || "auto",
    pointerEvents: (element._style && element._style.pointerEvents) || "auto",
    ...(element._style || {}),
  };
}

class TestElement {}

const context = {
  console,
  window: {
    scrollX: 0,
    scrollY: 0,
    innerHeight: 800,
    innerWidth: 1200,
    getComputedStyle,
  },
  document: {
    documentElement: {},
    querySelector() {
      return null;
    },
  },
  MutationObserver: class {
    observe() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  },
  Element: TestElement,
  ShadowRoot: class {},
  Node: { ELEMENT_NODE: 1, TEXT_NODE: 3 },
};
context.global = context;
vm.runInNewContext(
  `${src}
this.__exports = {
  isInteractable,
  isKendoDropdownValueTrigger,
};`,
  context,
);

function el(opts) {
  const element = new TestElement();
  element.tagName = (opts.tagName || "DIV").toUpperCase();
  element.nodeName = element.tagName;
  element.type = opts.type || "";
  element.disabled = Boolean(opts.disabled);
  element.hidden = Boolean(opts.hidden);
  element.className = "className" in opts ? opts.className : "";
  element.classList = opts.classList || [];
  element.textContent = opts.textContent || "";
  element.innerText = opts.innerText || element.textContent;
  element.parentElement = opts.parentElement || null;
  element.parentNode = element.parentElement;
  element.previousElementSibling = null;
  element.firstChild = null;
  element.nextSibling = null;
  element.childElementCount = 0;
  element.children = [];
  element.childNodes = [];
  element.nodeType = 1;
  element.ownerDocument = {
    defaultView: { getComputedStyle },
    createRange() {
      return {
        selectNode() {},
        getBoundingClientRect() {
          return element._rect;
        },
      };
    },
  };
  element._style = opts.style || {};
  element._rect = opts.rect || { width: 100, height: 30, left: 10, top: 10 };
  element._attributes = opts.attributes || {};
  element.href = opts.href || "";
  element.isContentEditable = Boolean(opts.isContentEditable);
  element.getAttribute = function (name) {
    return this._attributes[name] || null;
  };
  element.hasAttribute = function (name) {
    return name in this._attributes;
  };
  element.getBoundingClientRect = function () {
    return this._rect;
  };
  element.getRootNode = function () {
    return { host: null };
  };
  element.matches = function () {
    return false;
  };
  // Minimal closest: walk parentElement matching `[attr="val"]` and `.class` selectors
  // (comma-separated), enough to exercise the disabled/readonly fail-closed guard and
  // the .k-picker ancestor scope.
  element.closest = function (selector) {
    const sels = selector.split(",").map((s) => s.trim());
    let cur = this;
    while (cur) {
      for (const s of sels) {
        const attr = s.match(/^\[([\w-]+)="([^"]*)"\]$/);
        if (attr) {
          if ((cur._attributes || {})[attr[1]] === attr[2]) return cur;
          continue;
        }
        if (s[0] === ".") {
          const classes = (cur.className || "").toString().split(/\s+/);
          if (classes.includes(s.slice(1))) return cur;
        }
      }
      cur = cur.parentElement;
    }
    return null;
  };
  // Minimal querySelector: depth-first descendant scan for a single `.class` selector.
  element.querySelector = function (selector) {
    const wanted = selector.slice(1);
    for (const child of this.children) {
      const classes = (child.className || "").toString().split(/\s+/);
      if (classes.includes(wanted)) {
        return child;
      }
      const found = child.querySelector(selector);
      if (found) {
        return found;
      }
    }
    return null;
  };
  element.checkVisibility = function () {
    return true;
  };
  element.contains = function (child) {
    let current = child;
    while (current) {
      if (current === this) {
        return true;
      }
      current = current.parentElement;
    }
    return false;
  };
  if (element.parentElement) {
    element.parentElement.children.push(element);
    element.parentElement.childElementCount =
      element.parentElement.children.length;
    element.parentElement.childNodes = element.parentElement.children;
  }
  return element;
}

let passed = 0;
let failed = 0;

function test(name, fn) {
  try {
    fn();
    passed++;
    process.stdout.write(`  PASS: ${name}\n`);
  } catch (e) {
    failed++;
    process.stderr.write(`  FAIL: ${name}\n    ${e.message}\n`);
  }
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

// The production shape: a Kendo DropDownList value span rendered loose in a toolbar,
// with a default cursor because open/close is delegated on the widget wrapper, so no
// directly-bound handler and no pointer cursor is visible on the span itself.
function kendoValueSpan(className) {
  const header = el({ tagName: "DIV", className: "document-grid-header" });
  return el({
    tagName: "SPAN",
    className: className,
    textContent: "Action",
    parentElement: header,
  });
}

test("Kendo DropDownList value span is interactable", () => {
  const span = kendoValueSpan("k-input-value-text");
  assert(
    context.__exports.isInteractable(span, new Map()) === true,
    "span.k-input-value-text should be surfaced as interactable",
  );
});

test("Kendo value span among multiple classes is interactable", () => {
  const span = kendoValueSpan("k-input-value-text k-text-ellipsis");
  assert(
    context.__exports.isInteractable(span, new Map()) === true,
    "the k-input-value-text token should be matched among sibling classes",
  );
});

test("ordinary span stays non-interactable", () => {
  const header = el({ tagName: "DIV", className: "document-grid-header" });
  const span = el({
    tagName: "SPAN",
    textContent: "Just a label",
    parentElement: header,
  });
  assert(
    context.__exports.isInteractable(span, new Map()) === false,
    "a plain span should not be over-included as interactable",
  );
});

test("substring-only class token does not expose a span", () => {
  const span = kendoValueSpan("k-input-value");
  assert(
    context.__exports.isInteractable(span, new Map()) === false,
    "a span whose class only substring-matches should stay non-interactable",
  );
});

test("k-input-value-text on a non-span tag is not exposed", () => {
  const div = el({
    tagName: "DIV",
    className: "k-input-value-text",
    textContent: "Action",
  });
  assert(
    context.__exports.isInteractable(div, new Map()) === false,
    "the value-trigger seam should be scoped to spans, not any element",
  );
});

test("Kendo value span with pointer-events:none stays non-interactable", () => {
  const header = el({ tagName: "DIV", className: "document-grid-header" });
  const span = el({
    tagName: "SPAN",
    className: "k-input-value-text",
    textContent: "Action",
    parentElement: header,
    style: { pointerEvents: "none" },
  });
  assert(
    context.__exports.isInteractable(span, new Map()) === false,
    "pointer-events:none must keep the value span excluded",
  );
});

test("Kendo value span inside aria-disabled ancestor is fail-closed", () => {
  const wrapper = el({
    tagName: "SPAN",
    className: "k-dropdownlist k-picker",
    attributes: { "aria-disabled": "true" },
  });
  const span = el({
    tagName: "SPAN",
    className: "k-input-value-text",
    textContent: "Locked Action",
    parentElement: wrapper,
  });
  assert(
    context.__exports.isInteractable(span, new Map()) === false,
    "value span of an aria-disabled widget must not be surfaced",
  );
});

test("Kendo value span inside k-disabled ancestor is fail-closed", () => {
  const wrapper = el({
    tagName: "SPAN",
    className: "k-dropdownlist k-picker k-disabled",
  });
  const span = el({
    tagName: "SPAN",
    className: "k-input-value-text",
    textContent: "Locked Action",
    parentElement: wrapper,
  });
  assert(
    context.__exports.isInteractable(span, new Map()) === false,
    "value span inside a k-disabled wrapper must not be surfaced",
  );
});

// Kendo's full picker shape: <span class="k-dropdownlist k-picker"> wrapping a
// <span class="k-input-inner"> that only gains a <span class="k-input-value-text"> child
// once the picker holds a value.
function kendoPickerWrapper(className, attributes) {
  const host = el({ tagName: "DIV", className: "document-grid-header" });
  return el({
    tagName: "SPAN",
    className: className ?? "k-dropdownlist k-picker",
    attributes: attributes ?? {},
    parentElement: host,
  });
}

function kendoInnerSpan(wrapper, textContent) {
  return el({
    tagName: "SPAN",
    className: "k-input-inner",
    textContent: textContent,
    parentElement: wrapper,
  });
}

test("unset Kendo picker surfaces its placeholder inner span", () => {
  const inner = kendoInnerSpan(kendoPickerWrapper(), "Select action...");
  assert(
    context.__exports.isInteractable(inner, new Map()) === true,
    "a picker with no value renders no value-text child, so the inner span is the only aim-able trigger",
  );
});

test("valued Kendo picker surfaces the value span, not its inner", () => {
  const inner = kendoInnerSpan(kendoPickerWrapper(), "Action");
  const valueText = el({
    tagName: "SPAN",
    className: "k-input-value-text",
    textContent: "Action",
    parentElement: inner,
  });
  assert(
    context.__exports.isInteractable(valueText, new Map()) === true,
    "the value span stays the surfaced trigger once the picker holds a value",
  );
  assert(
    context.__exports.isInteractable(inner, new Map()) === false,
    "surfacing the inner too would nest two triggers and strand the outer one with trimmed text",
  );
});

test("k-input-inner outside a .k-picker stays non-interactable", () => {
  const header = el({ tagName: "DIV", className: "document-grid-header" });
  const inner = kendoInnerSpan(header, "Select action...");
  assert(
    context.__exports.isInteractable(inner, new Map()) === false,
    "the inner-span arm must stay scoped to Kendo pickers",
  );
});

test("ComboBox inner input is not claimed by the picker seam", () => {
  const wrapper = el({ tagName: "SPAN", className: "k-combobox k-input" });
  const input = el({
    tagName: "INPUT",
    className: "k-input-inner",
    attributes: { type: "text" },
    parentElement: wrapper,
  });
  assert(
    context.__exports.isKendoDropdownValueTrigger(input) === false,
    "a ComboBox's inner is an <input>, already covered by the ordinary input path",
  );
  assert(
    context.__exports.isInteractable(input, new Map()) === true,
    "the ComboBox input stays interactable on its own merits",
  );
});

test("span inner under a ComboBox wrapper stays non-interactable", () => {
  const wrapper = el({ tagName: "SPAN", className: "k-combobox k-input" });
  const inner = kendoInnerSpan(wrapper, "Select action...");
  assert(
    context.__exports.isInteractable(inner, new Map()) === false,
    "a ComboBox wrapper carries k-input, not k-picker, so the seam must not reach it",
  );
});

test("Kendo value span inside k-readonly wrapper is fail-closed", () => {
  const wrapper = kendoPickerWrapper("k-dropdownlist k-picker k-readonly");
  const span = el({
    tagName: "SPAN",
    className: "k-input-value-text",
    textContent: "Frozen Action",
    parentElement: kendoInnerSpan(wrapper, "Frozen Action"),
  });
  assert(
    context.__exports.isInteractable(span, new Map()) === false,
    "a readonly picker never binds its open handler, so its value span is an unclickable target",
  );
});

test("Kendo value span inside aria-readonly ancestor is fail-closed", () => {
  const wrapper = kendoPickerWrapper("k-dropdownlist k-picker", {
    "aria-readonly": "true",
  });
  const span = el({
    tagName: "SPAN",
    className: "k-input-value-text",
    textContent: "Frozen Action",
    parentElement: kendoInnerSpan(wrapper, "Frozen Action"),
  });
  assert(
    context.__exports.isInteractable(span, new Map()) === false,
    "value span of an aria-readonly widget must not be surfaced",
  );
});

test('aria-readonly="false" does not fail closed', () => {
  const wrapper = kendoPickerWrapper("k-dropdownlist k-picker", {
    "aria-readonly": "false",
  });
  const span = el({
    tagName: "SPAN",
    className: "k-input-value-text",
    textContent: "Action",
    parentElement: kendoInnerSpan(wrapper, "Action"),
  });
  assert(
    context.__exports.isInteractable(span, new Map()) === true,
    "Kendo stamps aria-readonly=false on every editable picker, so the guard must match the value, not the attribute",
  );
});

test("outer readonly container does not hide an editable Kendo picker", () => {
  const readonlyContainer = el({
    tagName: "DIV",
    attributes: { "aria-readonly": "true" },
  });
  const wrapper = el({
    tagName: "SPAN",
    className: "k-dropdownlist k-picker",
    attributes: { "aria-readonly": "false" },
    parentElement: readonlyContainer,
  });
  const span = el({
    tagName: "SPAN",
    className: "k-input-value-text",
    textContent: "Action",
    parentElement: kendoInnerSpan(wrapper, "Action"),
  });
  assert(
    context.__exports.isInteractable(span, new Map()) === true,
    "an unrelated readonly ancestor must not override the editable Kendo wrapper",
  );
});

test("outer k-readonly container does not hide an editable Kendo picker", () => {
  const readonlyContainer = el({
    tagName: "DIV",
    className: "k-readonly",
  });
  const wrapper = el({
    tagName: "SPAN",
    className: "k-dropdownlist k-picker",
    attributes: { "aria-readonly": "false" },
    parentElement: readonlyContainer,
  });
  const span = el({
    tagName: "SPAN",
    className: "k-input-value-text",
    textContent: "Action",
    parentElement: kendoInnerSpan(wrapper, "Action"),
  });
  assert(
    context.__exports.isInteractable(span, new Map()) === true,
    "an unrelated k-readonly ancestor must not override the editable Kendo wrapper",
  );
});

test("nearest nested Kendo picker owns readonly state", () => {
  const outerPicker = kendoPickerWrapper("k-dropdownlist k-picker k-readonly", {
    "aria-readonly": "true",
  });
  const innerPicker = el({
    tagName: "SPAN",
    className: "k-dropdownlist k-picker",
    attributes: { "aria-readonly": "false" },
    parentElement: outerPicker,
  });
  const span = el({
    tagName: "SPAN",
    className: "k-input-value-text",
    textContent: "Nested Action",
    parentElement: kendoInnerSpan(innerPicker, "Nested Action"),
  });
  assert(
    context.__exports.isInteractable(span, new Map()) === true,
    "the nearest picker must own readonly state in a nested picker tree",
  );
});

test("disabled state remains ancestor-wide across nested Kendo pickers", () => {
  const disabledOuterPicker = kendoPickerWrapper(
    "k-dropdownlist k-picker k-disabled",
    { "aria-disabled": "true" },
  );
  const innerPicker = el({
    tagName: "SPAN",
    className: "k-dropdownlist k-picker",
    attributes: { "aria-readonly": "false" },
    parentElement: disabledOuterPicker,
  });
  const span = el({
    tagName: "SPAN",
    className: "k-input-value-text",
    textContent: "Nested Action",
    parentElement: kendoInnerSpan(innerPicker, "Nested Action"),
  });
  assert(
    context.__exports.isInteractable(span, new Map()) === false,
    "an outer disabled picker must fail closed even when the nearest picker is editable",
  );
});

console.log(`\n${passed + failed} tests: ${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
