/**
 * Regression tests for DataTables Select per-row checkbox cells in domUtils.js.
 *
 * The Select extension renders a per-row selection control as
 * <td class="select-checkbox"> whose click is delegated on the ancestor <table>,
 * so the cell has no directly-bound handler. isInteractable must still surface it,
 * while ordinary <td> cells stay non-interactable.
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
  element.closest = function () {
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

// The production shape: a DataTables Select per-row checkbox cell. The class carries
// a leading space, no inline onclick, and a default cursor because the click is
// delegated on the ancestor <table>.
function selectCheckboxCell() {
  const table = el({ tagName: "TABLE", className: "dataTable" });
  const row = el({ tagName: "TR", parentElement: table });
  return el({
    tagName: "TD",
    className: " select-checkbox",
    parentElement: row,
  });
}

test("DataTables Select per-row checkbox cell is interactable", () => {
  const cell = selectCheckboxCell();
  assert(
    context.__exports.isInteractable(cell, new Map()) === true,
    "td.select-checkbox should be surfaced as interactable",
  );
});

test("ordinary table cell stays non-interactable", () => {
  const table = el({ tagName: "TABLE" });
  const row = el({ tagName: "TR", parentElement: table });
  const cell = el({
    tagName: "TD",
    textContent: "Row cell text",
    parentElement: row,
  });
  assert(
    context.__exports.isInteractable(cell, new Map()) === false,
    "a plain td should not be over-included as interactable",
  );
});

test("substring-only class token does not expose a table cell", () => {
  const table = el({ tagName: "TABLE" });
  const row = el({ tagName: "TR", parentElement: table });
  const cell = el({
    tagName: "TD",
    className: "select",
    parentElement: row,
  });
  assert(
    context.__exports.isInteractable(cell, new Map()) === false,
    "a td whose class only substring-matches should stay non-interactable",
  );
});

test("select-checkbox class on a non-cell tag is not exposed", () => {
  const wrapper = el({
    tagName: "DIV",
    className: "select-checkbox",
  });
  assert(
    context.__exports.isInteractable(wrapper, new Map()) === false,
    "the select-checkbox seam should be scoped to table cells, not any element",
  );
});

console.log(`\n${passed + failed} tests: ${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
