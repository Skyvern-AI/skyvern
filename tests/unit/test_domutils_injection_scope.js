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

// SKY-12875 M2: exports must not be interceptable. `globalThis.x = x` invokes a page-installed
// setter and hands it our genuine function, which let a page serve a wrapper from a getter and
// drive our own builder with arguments we never passed (forcing destination capture on while the
// policy was disabled). These drive the real generated script, not its source text.

check("a page setter never receives an exported function", () => {
  const ctx = makeContext();
  vm.runInContext(
    `globalThis.__stolen = null;
     Object.defineProperty(globalThis, "buildTreeFromBody", {
       configurable: true,
       get() { return globalThis.__stolen; },
       set(value) { globalThis.__stolen = value; },
     });`,
    ctx,
  );
  indirectEval(ctx, wrappedScript);
  assert.strictEqual(
    ctx.__stolen,
    null,
    "the page's setter was invoked by the export",
  );
  assert.strictEqual(
    typeof ctx.buildTreeFromBody,
    "function",
    "the export did not install our function",
  );
});

check("a non-configurable writable global does not break the export", () => {
  // This is the descriptor a real page's top-level `var element = 1` produces, and several
  // exported names are ordinary words — so it is ordinary sites, not attack, and refusing it
  // would break scraping everywhere. Written as an explicit descriptor because `var` inside a vm
  // context is configurable, unlike a browser's; the real-browser case is covered by a Chromium
  // probe.
  const ctx = makeContext();
  vm.runInContext(
    `Object.defineProperty(globalThis, "buildTreeFromBody", {
       value: 1, writable: true, enumerable: true, configurable: false,
     });`,
    ctx,
  );
  indirectEval(ctx, wrappedScript);
  assert.strictEqual(typeof ctx.buildTreeFromBody, "function");
});

check("a locked accessor on an exported global fails injection loudly", () => {
  const ctx = makeContext();
  vm.runInContext(
    `Object.defineProperty(globalThis, "buildTreeFromBody", {
       configurable: false,
       get() { return undefined; },
       set(value) { globalThis.__stolen = value; },
     });`,
    ctx,
  );
  assert.throws(
    () => indirectEval(ctx, wrappedScript),
    /refusing to export over locked global/,
    "injection must refuse rather than run through an interposed global",
  );
  assert.strictEqual(ctx.__stolen, undefined, "the page captured our function");
});

process.exit(failures ? 1 : 0);
