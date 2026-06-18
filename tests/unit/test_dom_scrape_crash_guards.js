/**
 * Regression tests for two domUtils.js scraper crash paths: undefined className
 * in isHoverPointerElement (TypeError) and the isElementVisible display:contents
 * recursion cycle (RangeError). Exit 0 = pass, exit 1 = failures on stderr.
 */

const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(
  path.join(__dirname, "../../skyvern/webeye/scraper/domUtils.js"),
  "utf8",
);

function extract(name) {
  const fnStart = src.indexOf(`function ${name}(`);
  if (fnStart === -1) throw new Error(`${name} not found`);
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
  return src.substring(fnStart, fnEnd);
}

global.ShadowRoot = class {};

function getElementComputedStyle(el) {
  return {
    display: "block",
    visibility: "visible",
    opacity: "1",
    cursor: (el._style && el._style.cursor) || "auto",
    ...(el._style || {}),
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
  "isVisibleTextNode",
  "ShadowRoot",
  `${extract("isElementVisible")}\nreturn isElementVisible;`,
)(
  getElementComputedStyle,
  () => true,
  () => false,
  () => false,
  () => false,
  () => false,
  () => null,
  () => false,
  global.ShadowRoot,
);

const isHoverPointerElement = new Function(
  "getElementComputedStyle",
  `${extract("isHoverPointerElement")}\nreturn isHoverPointerElement;`,
)(getElementComputedStyle);

function el(opts) {
  return {
    tagName: (opts.tagName || "DIV").toUpperCase(),
    type: opts.type || "",
    disabled: false,
    className: "className" in opts ? opts.className : "",
    classList: opts.classList || [],
    parentElement: opts.parentElement || null,
    previousElementSibling: null,
    nodeType: 1,
    firstChild: null,
    nextSibling: null,
    _style: opts.style || {},
    _rect: opts.rect || { width: 100, height: 30, left: 10, top: 10 },
    _attributes: opts.attributes || {},
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
    matches() {
      return false;
    },
    closest() {
      return null;
    },
  };
}

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

// --- className === undefined must not throw ---
test("isHoverPointerElement: undefined className does not throw", () => {
  const e = el({
    tagName: "DIV",
    className: undefined,
    style: { cursor: "auto" },
  });
  // Throws on the unguarded version; caught by test() and reported as FAIL.
  const result = isHoverPointerElement(e, new Map());
  assert(
    result === false,
    "undefined className + cursor:auto => not hover-pointer",
  );
});

test("isHoverPointerElement: normal className still detected", () => {
  const e = el({
    tagName: "DIV",
    className: "btn hover:cursor-pointer",
    style: { cursor: "auto" },
  });
  assert(
    isHoverPointerElement(e, new Map()) === true,
    "hover:cursor-pointer class => true",
  );
});

test("isHoverPointerElement: SVG className (SVGAnimatedString) matched via baseVal", () => {
  // SVG className is an object; toString() => "[object SVGAnimatedString]", so the
  // class match must read baseVal or the hover class is missed.
  const e = el({
    tagName: "svg",
    className: {
      baseVal: "icon hover:cursor-pointer",
      toString: () => "[object SVGAnimatedString]",
    },
    style: { cursor: "auto" },
  });
  assert(
    isHoverPointerElement(e, new Map()) === true,
    "SVG hover:cursor-pointer (in baseVal) should be detected",
  );
});

// --- display:contents + form control must not overflow the stack ---
test("isElementVisible: display:contents parent containing a checkbox does not overflow", () => {
  const parent = el({ tagName: "DIV", style: { display: "contents" } });
  const checkbox = el({
    tagName: "INPUT",
    type: "checkbox",
    parentElement: parent,
  });
  parent.firstChild = checkbox;
  // Both directions overflow the stack on the unguarded version.
  const fromChild = isElementVisible(checkbox);
  const fromParent = isElementVisible(parent);
  assert(
    typeof fromChild === "boolean" && typeof fromParent === "boolean",
    "both evaluations terminate and return a boolean",
  );
});

test("isElementVisible: display:contents parent containing an option does not overflow", () => {
  const parent = el({ tagName: "SPAN", style: { display: "contents" } });
  const option = el({ tagName: "OPTION", parentElement: parent });
  parent.firstChild = option;
  const result = isElementVisible(option);
  assert(
    typeof result === "boolean",
    "evaluation terminates and returns a boolean",
  );
});

console.log(`\n${passed + failed} tests: ${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
