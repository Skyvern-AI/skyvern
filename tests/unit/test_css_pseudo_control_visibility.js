/**
 * Behavioral tests: CSS pseudo-content zero-rect clickable control
 * visibility.
 *
 * Verifies that isElementVisible correctly handles:
 *  - icon buttons with own cursor:pointer + pseudo-content + zero rect (visible)
 *  - decorative pseudo under inherited cursor:pointer (NOT visible)
 *  - normal-rect elements with pseudo-content (unchanged, visible)
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
const fnStart = src.indexOf("function isElementVisible(element) {");
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
global.ShadowRoot = class {};
global.window = { scrollX: 0 };

function makeEl(opts) {
  return {
    tagName: (opts.tagName || "DIV").toUpperCase(),
    type: opts.type || "",
    disabled: false,
    className: opts.className || "",
    parentElement: opts.parentElement || null,
    previousElementSibling: null,
    _attributes: opts.attributes || {},
    _rect: opts.rect || { width: 100, height: 30, left: 10, top: 10 },
    _style: opts.style || {},
    _pseudoBefore: opts.pseudoBefore || null,
    _pseudoAfter: opts.pseudoAfter || null,
    getAttribute(n) {
      return this._attributes[n] || null;
    },
    getBoundingClientRect() {
      return this._rect;
    },
    getRootNode() {
      return { host: null };
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

function getElementComputedStyle(el) {
  return {
    display: "block",
    visibility: "visible",
    opacity: "1",
    cursor: el._style.cursor || "auto",
    ...el._style,
  };
}

const isElementVisible = new Function(
  "getElementComputedStyle",
  "isElementStyleVisibilityVisible",
  "isHoverOnlyElement",
  "isHidden",
  "isScriptOrStyle",
  "hasBeforeOrAfterPseudoContent",
  "getPseudoContent",
  "ShadowRoot",
  `${fnSource}\nreturn isElementVisible;`,
)(
  getElementComputedStyle,
  () => true,
  () => false,
  () => false,
  () => false,
  (el) => el._pseudoBefore != null || el._pseudoAfter != null,
  (el, p) => (p === "::before" ? el._pseudoBefore : el._pseudoAfter),
  global.ShadowRoot,
);

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

test("zero-rect + own cursor:pointer + pseudo-content: IS visible", () => {
  const parent = makeEl({ tagName: "DIV", style: { cursor: "auto" } });
  const iconBtn = makeEl({
    tagName: "DIV",
    rect: { width: 0, height: 0, left: -30, top: 53 },
    style: { cursor: "pointer" },
    pseudoAfter: "\uf061",
    parentElement: parent,
  });
  assert(isElementVisible(iconBtn), "icon button should be visible");
});

test("zero-rect + inherited cursor:pointer + pseudo: NOT visible", () => {
  const button = makeEl({
    tagName: "BUTTON",
    style: { cursor: "pointer" },
  });
  const span = makeEl({
    tagName: "SPAN",
    rect: { width: 0, height: 0, left: 0, top: 0 },
    style: { cursor: "pointer" },
    pseudoBefore: "\u2022",
    parentElement: button,
  });
  assert(!isElementVisible(span), "inherited cursor should not bypass");
});

test("zero-rect + own cursor:pointer but NO pseudo: NOT visible", () => {
  const parent = makeEl({ tagName: "DIV", style: { cursor: "auto" } });
  const el = makeEl({
    tagName: "DIV",
    rect: { width: 0, height: 0, left: 0, top: 0 },
    style: { cursor: "pointer" },
    parentElement: parent,
  });
  assert(!isElementVisible(el), "no pseudo-content = not visible");
});

test("normal-rect + pseudo-content: unchanged (visible)", () => {
  const el = makeEl({
    tagName: "DIV",
    rect: { width: 64, height: 64, left: 10, top: 10 },
    style: { cursor: "pointer" },
    pseudoAfter: "\uf061",
  });
  assert(isElementVisible(el), "normal rect element is always visible");
});

test("zero-rect + no parent (top-level) + cursor:pointer + pseudo: IS visible", () => {
  const el = makeEl({
    tagName: "DIV",
    rect: { width: 0, height: 0, left: -30, top: 0 },
    style: { cursor: "pointer" },
    pseudoAfter: "\uf061",
    parentElement: null,
  });
  assert(
    isElementVisible(el),
    "top-level icon with no parent should be visible",
  );
});

console.log(`\n${passed + failed} tests: ${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
