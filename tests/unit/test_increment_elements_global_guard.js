/**
 * Regression test for the getIncrementElements crash path in domUtils.js: a
 * mid-click context reset can wipe window.globalParsedElementCounter, and the
 * wait loop dereferenced `.get()` on undefined and threw a TypeError. Waiting
 * now tracks pending entries independently of the parsed counter.
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

const OBSERVER_VERSION_MATCH = src.match(
  /const INCREMENTAL_OBSERVER_VERSION = (\d+);/,
);
if (!OBSERVER_VERSION_MATCH)
  throw new Error("INCREMENTAL_OBSERVER_VERSION not found");
const OBSERVER_VERSION = Number(OBSERVER_VERSION_MATCH[1]);

const makeGetIncrementElements = new Function(
  "window",
  "asyncSleepFor",
  "_jsConsoleError",
  "document",
  "buildElementObject",
  "stampOtpInputBoxes",
  "otpContainerCounts",
  // getIncrementElements now drains through the observer-aware helpers, so bind them (and the version
  // const the exact-match check closes over) too.
  `const INCREMENTAL_OBSERVER_VERSION = ${OBSERVER_VERSION};\n` +
    `${extractFn("isCurrentIncrementalObserver")}\n${extractFn("hasSplicingObserverContract")}\n` +
    `${extractFn("waitForIncrementalDrain")}\n` +
    `${extractFn("getIncrementElements")}\nreturn getIncrementElements;`,
);

const CURRENT_OBSERVER = { skyvernObserverVersion: OBSERVER_VERSION };

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
    () => {},
    new WeakMap(),
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

  await test("getIncrementElements: undefined pending entries with waiting enabled is safe", async () => {
    const win = {
      globalOneTimeIncrementElements: undefined,
      globalDomDepthMap: new Map(),
    };
    assertEmptyResult(await bind(win)(true));
  });

  await test("getIncrementElements: current observer waits for pending work without a parsed counter", async () => {
    let waited = false;
    const win = {
      globalObserverForDOMIncrement: CURRENT_OBSERVER,
      globalParsedElementCounter: undefined,
      globalOneTimeIncrementElements: [{}],
      globalDomDepthMap: new Map(),
    };
    const result = await bind(win, async () => {
      waited = true;
      win.globalOneTimeIncrementElements.pop();
    })(true);
    assert(waited, "read must wait for pending work to finish");
    assertEmptyResult(result);
  });

  await test("getIncrementElements: legacy observer drains by parsed counter, not array length", async () => {
    let sleeps = 0;
    const win = {
      globalObserverForDOMIncrement: {}, // unmarked -> legacy contract
      globalParsedElementCounter: { get: async () => win._parsed },
      _parsed: 0,
      globalOneTimeIncrementElements: [{}, {}], // monotonic history the legacy callback never splices
      globalDomDepthMap: new Map(),
    };
    const result = await bind(win, async () => {
      sleeps += 1;
      win._parsed = win.globalOneTimeIncrementElements.length;
    })(true);
    assert(
      sleeps === 1,
      "legacy drain must wait until the parsed counter catches the history length",
    );
    assertEmptyResult(result);
  });

  await test("getIncrementElements: unstamped splicing observer drains by pending length, not the parsed counter", async () => {
    let sleeps = 0;
    const win = {
      globalObserverForDOMIncrement: {}, // unstamped, but a transitional splicing build
      globalIncrementalJobCount: 3, // scalar already bumped -> splicing contract proven
      globalParsedElementCounter: { get: async () => 9 }, // counter raced past the pending length
      globalOneTimeIncrementElements: [{}], // one job still in flight, not yet spliced
      globalDomDepthMap: new Map(),
    };
    const result = await bind(win, async () => {
      sleeps += 1;
      win.globalOneTimeIncrementElements.pop();
    })(true);
    // The pre-splice predicate (parsed 9 >= length 1) would exit immediately and abandon the pending
    // entry; the splicing contract must wait for the array to drain instead.
    assert(
      sleeps === 1,
      "splicing observer must wait for the pending entry to splice out, not exit on the parsed counter",
    );
    assertEmptyResult(result);
  });

  await test("getIncrementElements: drain reevaluates the contract once the scalar proves splicing", async () => {
    let sleeps = 0;
    const win = {
      globalObserverForDOMIncrement: {}, // unstamped
      globalIncrementalJobCount: 0, // idle at entry -> pre-splice contract
      globalParsedElementCounter: { get: async () => win._parsed },
      _parsed: 0,
      globalOneTimeIncrementElements: [{}], // a pending entry from a job about to prove itself
      globalDomDepthMap: new Map(),
    };
    const result = await bind(win, async () => {
      sleeps += 1;
      if (sleeps === 1) {
        // first job completes: bumps the scalar and races the parsed counter past the pending length
        win.globalIncrementalJobCount = 1;
        win._parsed = 5;
      } else {
        win.globalOneTimeIncrementElements.pop();
      }
    })(true);
    // A contract captured once at entry would stay pre-splice and exit on the raced counter, leaking
    // the pending entry; reevaluating each poll switches to pending-length draining after the bump.
    assert(
      sleeps === 2,
      "drain must switch to pending-length semantics after the scalar proves splicing",
    );
    assertEmptyResult(result);
  });

  console.log(`\n${passed + failed} tests: ${passed} passed, ${failed} failed`);
  process.exit(failed > 0 ? 1 : 0);
})();
