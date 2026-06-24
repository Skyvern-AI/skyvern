/**
 * Behavioral regression tests for getElementText extracting bare text nodes
 * that live directly inside an element's open shadow root.
 *
 * Web components can render values as a bare text node child of the shadow
 * root, with no wrapping element. The scraper walks shadow Element children
 * separately, so the text node must be folded into the host's `text` here or
 * the value never reaches the element tree.
 *
 * Exit 0 = pass, exit 1 = failures on stderr.
 */

// domUtils.js is a browser script with top-level references to `window`,
// `Node`, and `MutationObserver`. Run it in an isolated vm context with
// minimal shims so we can grab function references without polluting the
// Node process globals — and without tripping the export guard, which
// intentionally bails when `window` is defined.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(
  path.join(__dirname, "../../skyvern/webeye/scraper/domUtils.js"),
  "utf8",
);

const context = vm.createContext({
  window: {},
  Node: { TEXT_NODE: 3, ELEMENT_NODE: 1 },
  MutationObserver: function () {},
  console,
});
vm.runInContext(src, context);

const { getElementText } = context;

function textNode(data) {
  return { nodeType: 3, data };
}

function elementNode({ childNodes = [], shadowRoot = null } = {}) {
  return {
    nodeType: 1,
    childNodes,
    shadowRoot,
  };
}

function shadowRoot(childNodes) {
  return { childNodes };
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
function assertEq(actual, expected, msg) {
  if (actual !== expected) {
    throw new Error(
      `${msg}\n      expected: ${JSON.stringify(expected)}\n      actual:   ${JSON.stringify(actual)}`,
    );
  }
}

// --- Web-component pattern: shadow root contains a bare text node, host has empty light DOM ---
test("getElementText: web-component bare shadow text node", () => {
  const host = elementNode({
    childNodes: [],
    shadowRoot: shadowRoot([textNode("PURE-06925911")]),
  });
  assertEq(
    getElementText(host),
    "PURE-06925911",
    "host text must surface shadow root direct text node",
  );
});

test("getElementText: shadow text trimmed of whitespace", () => {
  const host = elementNode({
    childNodes: [],
    shadowRoot: shadowRoot([textNode("  PURE-06925911\n  ")]),
  });
  assertEq(
    getElementText(host),
    "PURE-06925911",
    "leading/trailing whitespace trimmed",
  );
});

test("getElementText: shadow text empty/whitespace-only ignored", () => {
  const host = elementNode({
    childNodes: [],
    shadowRoot: shadowRoot([textNode("   "), textNode("")]),
  });
  assertEq(
    getElementText(host),
    "",
    "no text contribution from empty shadow text nodes",
  );
});

// --- closed shadow root → element.shadowRoot is null → no contribution ---
test("getElementText: closed shadow root contributes nothing", () => {
  const host = elementNode({ childNodes: [], shadowRoot: null });
  assertEq(getElementText(host), "", "closed shadow root yields no host text");
});

// --- mixed: shadow root has both Text node and Element children. Text is folded; Element children
//     are walked by processElement separately and must NOT be picked up here. ---
test("getElementText: shadow Element children NOT folded into host text", () => {
  const innerSpan = elementNode({
    childNodes: [textNode("nested element text")],
  });
  const host = elementNode({
    childNodes: [],
    shadowRoot: shadowRoot([textNode("bare-shadow-text"), innerSpan]),
  });
  // Must include the bare shadow text; must NOT include the inner span's text
  // (that is the Element walker's job via the shadow walk path).
  assertEq(
    getElementText(host),
    "bare-shadow-text",
    "only direct shadow text, not nested element text",
  );
});

// --- <slot> is an Element child of shadowRoot. We must NOT walk into it; slotted content lives in
//     light DOM and is captured by Element walker traversing the host's light children. ---
test("getElementText: shadow root <slot> Element is not walked", () => {
  const slottedLight = elementNode({
    childNodes: [textNode("slotted-light-content")],
  });
  const slot = elementNode({ childNodes: [slottedLight] }); // simulates <slot> w/ assigned nodes
  const host = elementNode({
    childNodes: [],
    shadowRoot: shadowRoot([slot]),
  });
  assertEq(
    getElementText(host),
    "",
    "no double-counting of slotted content via shadow walk",
  );
});

// --- host also has direct light DOM text. Both light and shadow text are concatenated. ---
test("getElementText: light DOM text + shadow root text concatenated", () => {
  const host = elementNode({
    childNodes: [textNode("Label:")],
    shadowRoot: shadowRoot([textNode("VALUE-123")]),
  });
  assertEq(
    getElementText(host),
    "Label:;VALUE-123",
    "light + shadow text joined by ';'",
  );
});

// --- non-host element (no shadowRoot) still works exactly as before ---
test("getElementText: regular element light DOM only (regression)", () => {
  const span = elementNode({ childNodes: [textNode("plain text")] });
  assertEq(getElementText(span), "plain text", "light-DOM-only path unchanged");
});

test("getElementText: regular empty element (regression)", () => {
  const div = elementNode({ childNodes: [] });
  assertEq(getElementText(div), "", "empty light DOM still returns ''");
});

// --- nested shadow: each host folds its OWN shadow direct text. getElementText does not recurse. ---
test("getElementText: nested shadow hosts each own their own shadow text", () => {
  const innerHost = elementNode({
    childNodes: [],
    shadowRoot: shadowRoot([textNode("inner-value")]),
  });
  const outerHost = elementNode({
    childNodes: [],
    shadowRoot: shadowRoot([textNode("outer-value"), innerHost]),
  });
  // outerHost.text only contains outer's direct shadow text; inner is handled
  // when Element walker recurses to innerHost.
  assertEq(
    getElementText(outerHost),
    "outer-value",
    "no cross-layer recursion",
  );
});

// --- Text node passed in directly (the existing early-return branch) still works ---
test("getElementText: TEXT_NODE input returns its trimmed data (regression)", () => {
  const tn = { nodeType: 3, data: "  raw  " };
  assertEq(getElementText(tn), "raw", "TEXT_NODE branch unchanged");
});

// --- Production purity: target pages can leak a `module` global (UMD/browserify
//     shim); the injected scraper source must never write to module.exports,
//     regardless of what the page has set up. ---
test("production source never assigns to module.exports", () => {
  const pageContext = vm.createContext({
    window: {},
    Node: { TEXT_NODE: 3, ELEMENT_NODE: 1 },
    MutationObserver: function () {},
    console,
    module: { exports: { iAmThePage: true } },
  });
  vm.runInContext(src, pageContext);
  assertEq(
    pageContext.module.exports.iAmThePage,
    true,
    "page's module.exports must remain untouched",
  );
  assertEq(
    pageContext.module.exports.getElementText,
    undefined,
    "scraper must not leak its functions into the page",
  );
});

console.log(`\n${passed + failed} tests: ${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
