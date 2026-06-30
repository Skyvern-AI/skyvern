/**
 * Regression tests for datepicker navigation controls in domUtils.js.
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
  isDatepickerNavigationElement,
  isInteractable,
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
  element.closest = function (selector) {
    if (selector.includes("datepicker")) {
      let current = this.parentElement;
      while (current) {
        const className = current.className?.toString() || "";
        if (
          className.split(/\s+/).some((name) => name.includes("datepicker"))
        ) {
          return current;
        }
        current = current.parentElement;
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

test("Bootstrap datepicker prev/next headers are interactable", () => {
  const picker = el({ tagName: "DIV", className: "datepicker-dropdown" });
  const prev = el({
    tagName: "TH",
    className: "prev",
    textContent: "\u00ab",
    parentElement: picker,
  });
  const next = el({
    tagName: "TH",
    className: "next",
    textContent: "\u00bb",
    parentElement: picker,
  });
  assert(
    context.__exports.isDatepickerNavigationElement(prev) === true,
    "prev header should be recognized as datepicker navigation",
  );
  assert(
    context.__exports.isDatepickerNavigationElement(next) === true,
    "next header should be recognized as datepicker navigation",
  );
  assert(
    context.__exports.isInteractable(prev, new Map()) === true,
    "prev header should become interactable and keep a Skyvern id",
  );
  assert(
    context.__exports.isInteractable(next, new Map()) === true,
    "next header should become interactable and keep a Skyvern id",
  );
});

test("Bootstrap datepicker ancestry supports explicit datepicker class tokens", () => {
  const picker = el({ tagName: "DIV", className: "bootstrap-datepicker" });
  const month = el({
    tagName: "DIV",
    className: "datepicker-months",
    parentElement: picker,
  });
  const switcher = el({
    tagName: "TH",
    className: "datepicker-switch",
    textContent: "2026",
    parentElement: month,
  });
  assert(
    context.__exports.isDatepickerNavigationElement(switcher) === true,
    "datepicker-switch header should be recognized inside explicit datepicker class tokens",
  );
  assert(
    context.__exports.isInteractable(switcher, new Map()) === true,
    "datepicker-switch header should become interactable",
  );
});

test("ordinary table headers are not exposed as interactable", () => {
  const table = el({ tagName: "TABLE" });
  const header = el({
    tagName: "TH",
    textContent: "Created On",
    parentElement: table,
  });
  assert(
    context.__exports.isDatepickerNavigationElement(header) === false,
    "plain table header should not match datepicker navigation",
  );
  assert(
    context.__exports.isInteractable(header, new Map()) === false,
    "plain table header should stay non-interactable",
  );
});

test("substring-only datepicker ancestor classes do not expose table headers", () => {
  const table = el({ tagName: "TABLE", className: "notdatepicker" });
  const header = el({
    tagName: "TH",
    className: "prev",
    textContent: "\u00ab",
    parentElement: table,
  });
  assert(
    context.__exports.isDatepickerNavigationElement(header) === false,
    "prev header should not match inside a substring-only datepicker ancestor",
  );
  assert(
    context.__exports.isInteractable(header, new Map()) === false,
    "prev header inside a substring-only ancestor should stay non-interactable",
  );
});

test("disabled Bootstrap datepicker headers are not exposed as interactable", () => {
  const picker = el({ tagName: "DIV", className: "datepicker" });
  const prev = el({
    tagName: "TH",
    className: "prev disabled",
    textContent: "\u00ab",
    parentElement: picker,
  });
  const next = el({
    tagName: "TH",
    className: "next",
    textContent: "\u00bb",
    attributes: { "aria-disabled": "true" },
    parentElement: picker,
  });
  assert(
    context.__exports.isDatepickerNavigationElement(prev) === false,
    "prev disabled header should not be recognized as datepicker navigation",
  );
  assert(
    context.__exports.isDatepickerNavigationElement(next) === false,
    "aria-disabled next header should not be recognized as datepicker navigation",
  );
  assert(
    context.__exports.isInteractable(prev, new Map()) === false,
    "prev disabled header should stay non-interactable",
  );
  assert(
    context.__exports.isInteractable(next, new Map()) === false,
    "aria-disabled next header should stay non-interactable",
  );
});

test("datepicker ancestry walk is depth capped", () => {
  const picker = el({ tagName: "DIV", className: "datepicker" });
  let parent = picker;
  for (let i = 0; i < 25; i++) {
    parent = el({ tagName: "DIV", parentElement: parent });
  }
  const deeplyNestedPrev = el({
    tagName: "TH",
    className: "prev",
    textContent: "\u00ab",
    parentElement: parent,
  });
  assert(
    context.__exports.isDatepickerNavigationElement(deeplyNestedPrev) === false,
    "prev header should not match beyond the datepicker ancestor depth cap",
  );
  assert(
    context.__exports.isInteractable(deeplyNestedPrev, new Map()) === false,
    "prev header beyond the cap should stay non-interactable",
  );
});

console.log(`\n${passed + failed} tests: ${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
