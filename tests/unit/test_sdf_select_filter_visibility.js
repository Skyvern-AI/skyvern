/**
 * Behavioral tests: shadow DOM filter input visibility for closed
 * dropdown/select web components.
 *
 * Verifies that the isElementVisible guard correctly distinguishes:
 *  - filter inputs inside closed dropdowns (NOT force-visible)
 *  - regular shadow DOM text inputs (force-visible)
 *  - unrelated aria-expanded elements in the same shadow root (no effect)
 *  - open dropdown filter inputs (force-visible)
 *
 * Exit 0 = pass, exit 1 = failures on stderr.
 */

const fs = require("fs");
const path = require("path");

// ---------------------------------------------------------------------------
// Extract isElementVisible from domUtils.js
// ---------------------------------------------------------------------------
const src = fs.readFileSync(
  path.join(__dirname, "../../skyvern/webeye/scraper/domUtils.js"),
  "utf8",
);
const fnStart = src.indexOf("function isElementVisible(");
if (fnStart === -1) throw new Error("isElementVisible not found");
const bodyStart = src.indexOf("{", fnStart);
let depth = 0,
  fnEnd = -1;
for (let i = bodyStart; i < src.length; i++) {
  if (src[i] === "{") depth++;
  else if (src[i] === "}") {
    depth--;
    if (depth === 0) {
      fnEnd = i + 1;
      break;
    }
  }
}
const fnSource = src.substring(fnStart, fnEnd);

// ---------------------------------------------------------------------------
// Mock DOM primitives
// ---------------------------------------------------------------------------
class MockShadowRoot {
  constructor(host, children) {
    this._host = host;
    this._children = children || [];
  }
  get host() {
    return this._host;
  }
  querySelector(sel) {
    const m = sel.match(/\[aria-expanded="([^"]+)"\]/);
    if (!m) return null;
    for (const c of this._children) {
      if (c._attributes && c._attributes["aria-expanded"] === m[1]) return c;
    }
    return null;
  }
}
global.ShadowRoot = MockShadowRoot;
global.window = { scrollX: 0 };

function makeEl(opts) {
  const attrs = opts.attributes || {};
  return {
    tagName: (opts.tagName || "DIV").toUpperCase(),
    type: opts.type || "",
    disabled: false,
    className: opts.className || "",
    parentElement: opts.parentElement || null,
    previousElementSibling: opts.previousElementSibling || null,
    _attributes: attrs,
    _rect: opts.rect || { width: 100, height: 30, left: 10, top: 10 },
    _style: opts.style || {},
    _pseudoBefore: null,
    _pseudoAfter: null,
    getAttribute(n) {
      return this._attributes[n] || null;
    },
    getBoundingClientRect() {
      return this._rect;
    },
    getRootNode() {
      return opts.shadowRoot || { host: null };
    },
    hasAttribute(n) {
      return n in this._attributes;
    },
    closest() {
      return null;
    },
    firstChild: null,
  };
}

// Stubs
const stubs = {
  getElementComputedStyle: (el) => ({
    display: "block",
    visibility: "visible",
    opacity: "1",
    cursor: el._style.cursor || "auto",
  }),
  isElementStyleVisibilityVisible: () => true,
  isHoverOnlyElement: () => false,
  isHidden: () => false,
  getPseudoContent: () => null,
  hasBeforeOrAfterPseudoContent: () => false,
};

const isElementVisible = new Function(
  ...Object.keys(stubs),
  "ShadowRoot",
  `${fnSource}\nreturn isElementVisible;`,
)(...Object.values(stubs), MockShadowRoot);

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
let passed = 0,
  failed = 0;
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
function assert(c, m) {
  if (!c) throw new Error(m);
}

test("closed host: filter input NOT force-visible", () => {
  const host = makeEl({
    tagName: "custom-select",
    attributes: { "aria-expanded": "false" },
  });
  const sr = new MockShadowRoot(host, []);
  const input = makeEl({
    tagName: "INPUT",
    type: "text",
    shadowRoot: sr,
    rect: { width: 0, height: 0, left: 0, top: 0 },
  });
  assert(!isElementVisible(input), "should be hidden");
});

test("regular shadow DOM text input: IS force-visible", () => {
  const host = makeEl({ tagName: "custom-input", attributes: {} });
  const sr = new MockShadowRoot(host, []);
  const input = makeEl({ tagName: "INPUT", type: "text", shadowRoot: sr });
  assert(isElementVisible(input), "should be visible");
});

test("closed sibling trigger: combobox filter input NOT force-visible", () => {
  const host = makeEl({ tagName: "custom-select", attributes: {} });
  const trigger = makeEl({
    tagName: "DIV",
    attributes: { "aria-expanded": "false" },
  });
  const sr = new MockShadowRoot(host, [trigger]);
  const input = makeEl({
    tagName: "INPUT",
    type: "text",
    shadowRoot: sr,
    previousElementSibling: trigger,
    attributes: { role: "combobox" },
    rect: { width: 0, height: 0, left: 0, top: 0 },
  });
  assert(!isElementVisible(input), "combobox filter should be hidden");
});

test("non-combobox input with closed sibling: stays force-visible", () => {
  const host = makeEl({ tagName: "my-component", attributes: {} });
  const closedSection = makeEl({
    tagName: "DIV",
    attributes: { "aria-expanded": "false" },
  });
  const sr = new MockShadowRoot(host, [closedSection]);
  const input = makeEl({
    tagName: "INPUT",
    type: "text",
    shadowRoot: sr,
    previousElementSibling: closedSection,
  });
  assert(isElementVisible(input), "non-combobox input must not be suppressed");
});

test("unrelated closed element: does NOT suppress unrelated input", () => {
  const host = makeEl({ tagName: "my-component", attributes: {} });
  const accordion = makeEl({
    tagName: "DIV",
    attributes: { "aria-expanded": "false" },
  });
  const sr = new MockShadowRoot(host, [accordion]);
  const input = makeEl({
    tagName: "INPUT",
    type: "text",
    shadowRoot: sr,
    previousElementSibling: null,
  });
  assert(isElementVisible(input), "unrelated closed el must not suppress");
});

test("open dropdown (host expanded=true): input IS visible", () => {
  const host = makeEl({
    tagName: "custom-select",
    attributes: { "aria-expanded": "true" },
  });
  const sr = new MockShadowRoot(host, []);
  const input = makeEl({ tagName: "INPUT", type: "text", shadowRoot: sr });
  assert(isElementVisible(input), "open dropdown input should be visible");
});

console.log(`\n${passed + failed} tests: ${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
