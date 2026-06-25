/**
 * Regression test for the getIncrementElements crash path in domUtils.js: a
 * mid-click context reset can wipe window.globalParsedElementCounter, and the
 * wait loop dereferenced `.get()` on undefined and threw a TypeError. The guard
 * must skip the wait when the counter is gone and return an empty result.
 * Exit 0 = pass, exit 1 = failures on stderr.
 */

const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(
  path.join(__dirname, "../../skyvern/webeye/scraper/domUtils.js"),
  "utf8",
);

function extractFn(name) {
  let fnStart = src.indexOf(`async function ${name}(`);
  if (fnStart === -1) fnStart = src.indexOf(`function ${name}(`);
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

const makeGetIncrementElements = new Function(
  "window",
  "asyncSleepFor",
  "_jsConsoleError",
  "document",
  "buildElementObject",
  `${extractFn("getIncrementElements")}\nreturn getIncrementElements;`,
);

const immediateSleep = () => Promise.resolve();
const throwingDocument = {
  querySelector() {
    throw new Error(
      "document.querySelector should not run for an empty depth map",
    );
  },
};
const throwingBuild = () => {
  throw new Error("buildElementObject should not run for an empty depth map");
};

function bind(windowStub, sleep = immediateSleep) {
  return makeGetIncrementElements(
    windowStub,
    sleep,
    () => {},
    throwingDocument,
    throwingBuild,
  );
}

let passed = 0,
  failed = 0;
async function test(name, fn) {
  try {
    await fn();
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
function assertEmptyResult(result) {
  assert(Array.isArray(result) && result.length === 2, "result is a 2-tuple");
  assert(
    Array.isArray(result[0]) && result[0].length === 0,
    "elements list is empty",
  );
  assert(
    Array.isArray(result[1]) && result[1].length === 0,
    "tree list is empty",
  );
}

(async () => {
  // --- undefined counter after a context reset must not throw ---
  await test("getIncrementElements: undefined globalParsedElementCounter returns empty, no throw", async () => {
    const win = {
      globalParsedElementCounter: undefined,
      globalOneTimeIncrementElements: [],
      globalDomDepthMap: new Map(),
    };
    // Throws TypeError ("Cannot read properties of undefined (reading 'get')")
    // on the unguarded version; caught by test() and reported as FAIL.
    const result = await bind(win)(true);
    assertEmptyResult(result);
  });

  await test("getIncrementElements: undefined counter with wait_until_finished=false also safe", async () => {
    const win = {
      globalParsedElementCounter: undefined,
      globalOneTimeIncrementElements: undefined,
      globalDomDepthMap: new Map(),
    };
    const result = await bind(win)(false);
    assertEmptyResult(result);
  });

  // --- guard must not break the normal wait path when the counter exists ---
  await test("getIncrementElements: defined counter still waits until parsed catches up", async () => {
    let getCalls = 0;
    const win = {
      globalParsedElementCounter: {
        get: async () => (getCalls++ === 0 ? 0 : 1),
      },
      globalOneTimeIncrementElements: [{}],
      globalDomDepthMap: new Map(),
    };
    const result = await bind(win)(true);
    assert(
      getCalls >= 2,
      "wait loop ran at least one iteration before exiting",
    );
    assertEmptyResult(result);
  });

  console.log(`\n${passed + failed} tests: ${passed} passed, ${failed} failed`);
  process.exit(failed > 0 ? 1 : 0);
})();
