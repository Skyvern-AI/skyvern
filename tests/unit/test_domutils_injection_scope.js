// Behavioral test for domUtils.js injection scope isolation.
//
// page.evaluate runs string scripts through a sloppy indirect eval, which hoists
// top-level declarations into the page's global scope. When the target site's own
// JS holds a global lexical binding with the same name (e.g. `let uniqueId`),
// that hoist throws "SyntaxError: Identifier 'uniqueId' has already been declared"
// before any of our code runs. The loader must therefore ship domUtils.js wrapped
// in an isolated scope that exports entry points via property writes instead.
//
// Usage: node test_domutils_injection_scope.js <raw domUtils.js> <load_js_script() output>

const assert = require("node:assert");
const fs = require("node:fs");
const vm = require("node:vm");

const [, , rawPath, wrappedPath] = process.argv;
const rawScript = fs.readFileSync(rawPath, "utf8");
const wrappedScript = fs.readFileSync(wrappedPath, "utf8");

// Entry points invoked from Python snippets (handler.py, scraper.py, page.py,
// browser_ops.py) or by the captcha extension — all must stay reachable as
// globals after injection.
const ENTRY_POINTS = [
  "buildElementObject",
  "buildElementsAndDrawBoundingBoxes",
  "buildTreeFromBody",
  "captchaSolvedCallback",
  "getCaptchaSolves",
  "getElementDomDepth",
  "getHoverStylesMap",
  "getIncrementElements",
  "getScrollWidthAndHeight",
  "getScrollXY",
  "getSelectOptions",
  "isAnimationFinished",
  "isInteractable",
  "isWindowScrollable",
  "removeAllUniqueIds",
  "removeBoundingBoxes",
  "safeScrollToTop",
  "scrollNearestScrollableContainer",
  "scrollToElementBottom",
  "scrollToElementTop",
  "scrollToNextPage",
  "scrollToXY",
  "startGlobalIncrementalObserver",
  "stopGlobalIncrementalObserver",
  "uniqueId",
];

// Minimal browser surface touched by domUtils.js top-level statements.
function makeContext() {
  const sandbox = {
    console,
    MutationObserver: class {
      observe() {}
      disconnect() {}
    },
  };
  vm.createContext(sandbox);
  vm.runInContext("globalThis.window = globalThis;", sandbox);
  // The page's own script: a classic script whose top-level `let` creates a
  // persistent global lexical binding, exactly like a site-owned <script>.
  vm.runInContext("let uniqueId = 1;", sandbox);
  return sandbox;
}

// Mirrors Playwright's utilityScript `this.global.eval(expression)`.
function indirectEval(sandbox, code) {
  sandbox.__code = code;
  try {
    return vm.runInContext("(0, eval)(__code)", sandbox);
  } finally {
    delete sandbox.__code;
  }
}

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failures += 1;
    console.error(`FAIL - ${name}: ${err.message}`);
  }
}

check("raw domUtils.js collides with a page-owned lexical uniqueId", () => {
  assert.throws(
    () => indirectEval(makeContext(), rawScript),
    /already been declared/,
  );
});

check("loaded script survives a page-owned lexical uniqueId", () => {
  indirectEval(makeContext(), wrappedScript);
});

check("loaded script is re-injectable and exports all entry points", () => {
  const ctx = makeContext();
  indirectEval(ctx, wrappedScript);
  const counterAfterFirstInjection = ctx.elementIdCounter;
  assert.ok(
    counterAfterFirstInjection,
    "elementIdCounter should be initialized",
  );
  indirectEval(ctx, wrappedScript);
  assert.strictEqual(
    ctx.elementIdCounter,
    counterAfterFirstInjection,
    "re-injection must not reset elementIdCounter",
  );
  for (const name of ENTRY_POINTS) {
    assert.strictEqual(
      typeof ctx[name],
      "function",
      `${name} should be exported as a global function`,
    );
  }
});

process.exit(failures ? 1 : 0);
