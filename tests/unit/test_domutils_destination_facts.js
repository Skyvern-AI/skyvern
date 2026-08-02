/**
 * Behavioral tests for buildDestinationFacts in domUtils.js (SKY-12875).
 *
 * The facts are STALE, UNTRUSTED PREFLIGHT INPUT for the browser action firewall: a hostile page
 * controls every attribute read here, so the function must fail closed (null / url:null) on
 * anything malformed, clobbered, or throwing — never throw out of a scrape.
 * Exit 0 = pass, non-zero = failures on stderr.
 */

const assert = require("node:assert");
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(
  path.join(__dirname, "../../skyvern/webeye/scraper/domUtils.js"),
  "utf8",
);

function extract(name) {
  const fnStart = src.indexOf(`function ${name}(`);
  if (fnStart === -1) throw new Error(`${name} not found in domUtils.js`);
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

// buildDestinationFacts charges the per-build budget before it resolves, so it needs that state
// even to answer a shape question. These single-element checks are about the fact's SHAPE, so
// they run with an effectively unbounded budget; the per-build bound is exercised by buildLoop.
const buildDestinationFacts = new Function(
  `let __destinationFactBudget = Number.MAX_SAFE_INTEGER;
   ${extract("normalizeFormMethod")}
   ${extract("spendDestinationBudget")}
   ${extract("buildDestinationFacts")}
   return buildDestinationFacts;`,
)();

const DOC = {
  URL: "https://site.example/dir/page?q=1",
  baseURI: "https://site.example/dir/",
};

function stubElement({ tag, attrs = {}, form = undefined, doc = DOC }) {
  return {
    tagName: tag,
    form: form,
    ownerDocument: doc,
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(attrs, name)
        ? attrs[name]
        : null;
    },
  };
}

function stubForm(attrs = {}) {
  return {
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(attrs, name)
        ? attrs[name]
        : null;
    },
  };
}

const failures = [];
function check(name, fn) {
  try {
    fn();
    console.log(`ok: ${name}`);
  } catch (error) {
    failures.push(name);
    console.error(`FAIL: ${name}\n${error && error.message}`);
  }
}

check("relative anchor href resolves against the document base", () => {
  const facts = buildDestinationFacts(
    stubElement({ tag: "a", attrs: { href: "next/page" } }),
    "a",
  );
  assert.deepStrictEqual(facts, {
    kind: "anchor",
    url: "https://site.example/dir/next/page",
  });
});

check("absolute anchor href passes through", () => {
  const facts = buildDestinationFacts(
    stubElement({ tag: "a", attrs: { href: "https://other.example/x" } }),
    "a",
  );
  assert.deepStrictEqual(facts, {
    kind: "anchor",
    url: "https://other.example/x",
  });
});

check("anchor without href bears no destination structure", () => {
  assert.strictEqual(
    buildDestinationFacts(stubElement({ tag: "a" }), "a"),
    null,
  );
});

check("javascript: href is preserved for the policy layer to refuse", () => {
  const facts = buildDestinationFacts(
    stubElement({ tag: "a", attrs: { href: "javascript:void(0)" } }),
    "a",
  );
  assert.deepStrictEqual(facts, { kind: "anchor", url: "javascript:void(0)" });
});

check("unresolvable href is opaque, not a throw", () => {
  const facts = buildDestinationFacts(
    stubElement({
      tag: "a",
      attrs: { href: "http://[" },
      doc: { URL: "x", baseURI: "not a base" },
    }),
    "a",
  );
  assert.deepStrictEqual(facts, { kind: "anchor", url: null });
});

check("form-owned control resolves the owner's action and method", () => {
  const form = stubForm({ action: "/submit", method: "POST" });
  const facts = buildDestinationFacts(
    stubElement({ tag: "input", attrs: { type: "text" }, form }),
    "input",
  );
  assert.deepStrictEqual(facts, {
    kind: "form",
    url: "https://site.example/submit",
    method: "post",
  });
});

check(
  "missing or empty form action submits to the document URL per spec",
  () => {
    for (const attrs of [{}, { action: "" }]) {
      const facts = buildDestinationFacts(
        stubElement({
          tag: "input",
          attrs: { type: "text" },
          form: stubForm(attrs),
        }),
        "input",
      );
      assert.deepStrictEqual(facts, {
        kind: "form",
        url: "https://site.example/dir/page?q=1",
        method: "get",
      });
    }
  },
);

check("submitter formaction and formmethod override the owner", () => {
  const form = stubForm({ action: "/submit", method: "post" });
  const facts = buildDestinationFacts(
    stubElement({
      tag: "input",
      attrs: {
        type: "submit",
        formaction: "https://other.example/steal",
        formmethod: "dialog",
      },
      form,
    }),
    "input",
  );
  assert.deepStrictEqual(facts, {
    kind: "form",
    url: "https://other.example/steal",
    method: "dialog",
  });
});

check("a button with no type attribute is a submitter", () => {
  const form = stubForm({ action: "/submit" });
  const facts = buildDestinationFacts(
    stubElement({ tag: "button", attrs: { formaction: "/other" }, form }),
    "button",
  );
  assert.deepStrictEqual(facts, {
    kind: "form",
    url: "https://site.example/other",
    method: "get",
  });
});

check(
  "a form-owned type=button control is opaque: its click is script, not submission",
  () => {
    // The same control outside a form is opaque; an ancestor form must not lend it a known
    // destination. Non-submitting click controls stage no data and never submit, so classifying
    // them by the form's action would mark arbitrary script as a known, checkable destination.
    for (const type of ["button", "reset"]) {
      const form = stubForm({ action: "/submit" });
      const facts = buildDestinationFacts(
        stubElement({
          tag: "button",
          attrs: { type: type, formaction: "https://other.example/x" },
          form,
        }),
        "button",
      );
      assert.strictEqual(facts, null);
    }
  },
);

check("a form-owned reset input is opaque for the same reason", () => {
  const form = stubForm({ action: "/submit" });
  for (const type of ["button", "reset"]) {
    const facts = buildDestinationFacts(
      stubElement({ tag: "input", attrs: { type: type }, form }),
      "input",
    );
    assert.strictEqual(facts, null);
  }
});

check("a hidden ancestor form still owns its controls", () => {
  // display:none never enters the interactable tree, but element.form still resolves it and the
  // browser still submits through it; the real-browser smoke run proves the same end to end.
  const hiddenForm = stubForm({ action: "/submit-here", method: "post" });
  const facts = buildDestinationFacts(
    stubElement({ tag: "input", attrs: { type: "text" }, form: hiddenForm }),
    "input",
  );
  assert.deepStrictEqual(facts, {
    kind: "form",
    url: "https://site.example/submit-here",
    method: "post",
  });
});

check(
  "element.form is the effective owner: null owner means no destination",
  () => {
    // Covers both a control outside any form and a form= attribute naming a missing or non-form id,
    // which the .form IDL resolves to null rather than to the ancestor form.
    assert.strictEqual(
      buildDestinationFacts(
        stubElement({ tag: "input", attrs: { type: "text" }, form: null }),
        "input",
      ),
      null,
    );
  },
);

check(
  "a clobbered form (getAttribute shadowed by a named control) fails closed",
  () => {
    // HTMLFormElement's named getter is [LegacyOverrideBuiltIns]: <input name=getAttribute> shadows
    // the method. Trusting anything else the form says would trust the clobberer.
    const clobbered = { getAttribute: {} };
    assert.strictEqual(
      buildDestinationFacts(
        stubElement({ tag: "input", attrs: { type: "text" }, form: clobbered }),
        "input",
      ),
      null,
    );
  },
);

check("a throwing accessor never escapes the scrape", () => {
  const hostile = {
    tagName: "a",
    ownerDocument: DOC,
    getAttribute() {
      throw new Error("boom");
    },
  };
  assert.strictEqual(buildDestinationFacts(hostile, "a"), null);
});

check("an unknown form method normalizes to get", () => {
  const form = stubForm({ action: "/submit", method: "TELEPORT" });
  const facts = buildDestinationFacts(
    stubElement({ tag: "input", attrs: { type: "text" }, form }),
    "input",
  );
  assert.strictEqual(facts.method, "get");
});

check("a non-destination element yields no fact at all", () => {
  assert.strictEqual(
    buildDestinationFacts(stubElement({ tag: "div", form: undefined }), "div"),
    null,
  );
});

check("an invalid button type is a submitter, as browsers normalize it", () => {
  // The type attribute's invalid-value default is "submit": <button type=garbage formaction=evil>
  // SUBMITS to evil. Recording the owner form's safe action instead would be a fail-open —
  // policy would authorize a destination the browser never uses.
  const form = stubForm({ action: "/submit" });
  const facts = buildDestinationFacts(
    stubElement({
      tag: "button",
      attrs: { type: "garbage", formaction: "https://other.example/steal" },
      form,
    }),
    "button",
  );
  assert.deepStrictEqual(facts, {
    kind: "form",
    url: "https://other.example/steal",
    method: "get",
  });
});

check(
  "an invalid input type is not a submitter, as browsers normalize it to text",
  () => {
    const form = stubForm({ action: "/submit" });
    const facts = buildDestinationFacts(
      stubElement({
        tag: "input",
        attrs: { type: "garbage", formaction: "https://other.example/steal" },
        form,
      }),
      "input",
    );
    assert.deepStrictEqual(facts, {
      kind: "form",
      url: "https://site.example/submit",
      method: "get",
    });
  },
);

check(
  "a ping-bearing anchor is opaque: one URL cannot represent its destinations",
  () => {
    // <a ping=...> sends cross-origin POST beacons on activation, so a complete-looking href fact
    // would under-describe where a click sends data. Opaque classifies INCOMPLETE downstream.
    const facts = buildDestinationFacts(
      stubElement({
        tag: "a",
        attrs: { href: "/next", ping: "https://collector.example/beacon" },
      }),
      "a",
    );
    assert.deepStrictEqual(facts, { kind: "anchor", url: null });
  },
);

check("an oversized destination URL is opaque, not amplified", () => {
  // The action attribute is one page-supplied string, but the capture would duplicate it into
  // every owned control: capping stops O(controls x URL-length) payload amplification, and the
  // over-cap record degrades to INCOMPLETE rather than to trust.
  const huge = "https://site.example/" + "a".repeat(5000);
  const anchor = buildDestinationFacts(
    stubElement({ tag: "a", attrs: { href: huge } }),
    "a",
  );
  assert.deepStrictEqual(anchor, { kind: "anchor", url: null });
  const form = stubForm({ action: huge });
  const control = buildDestinationFacts(
    stubElement({ tag: "input", attrs: { type: "text" }, form }),
    "input",
  );
  assert.deepStrictEqual(control, { kind: "form", url: null, method: "get" });
});

check("a whitespace-only form action submits to the document URL (L1)", () => {
  // Same rule as the submitter override, on the owner form's own action attribute: the URL parser
  // strips leading/trailing C0-and-space, so "   " is an EMPTY action.
  const facts = buildDestinationFacts(
    stubElement({
      tag: "input",
      attrs: { type: "text" },
      form: stubForm({ action: "  \t\n " }),
    }),
    "input",
  );
  assert.deepStrictEqual(facts, {
    kind: "form",
    url: "https://site.example/dir/page?q=1",
    method: "get",
  });
});

check("the URL parser is never handed an over-cap base (M3b)", () => {
  // The base cap is a COST control, and the resolved cap hides it from the output: resolving
  // "next" against a 5000-char base produces an over-cap url either way, so only what the parser
  // was CALLED with distinguishes them. A local URL binding shadows the global for the extracted
  // code, which is how the argument becomes observable.
  // The budget is deliberately effectively unbounded here: it also refuses an over-cap base (its
  // length is what gets charged), so a binding budget would reject the huge base for the WRONG
  // reason and leave this green with the base cap deleted. `bounded` must be the only thing that
  // can reject, which is why the control run below has to prove the parser is reached at all.
  const spy = new Function(
    `const bases = [];
     class URL extends globalThis.URL {
       constructor(raw, base) {
         bases.push(base === undefined ? -1 : String(base).length);
         super(raw, base);
       }
     }
     let __destinationFactBudget = Number.MAX_SAFE_INTEGER;
     ${extract("normalizeFormMethod")}
     ${extract("spendDestinationBudget")}
     ${extract("buildDestinationFacts")}
     return { build: buildDestinationFacts, bases };`,
  )();
  const anchorIn = (baseURI) => ({
    tagName: "a",
    form: undefined,
    ownerDocument: { URL: "https://site.example/dir/page", baseURI },
    getAttribute: (name) => (name === "href" ? "next" : null),
  });
  // Non-vacuity control: an ordinary base must actually reach the parser. Without this, a builder
  // that throws before resolving leaves `bases` empty and `every` trivially true.
  spy.build(anchorIn("https://site.example/dir/"), "a");
  assert.ok(
    spy.bases.length > 0,
    "the parser was never reached: this check proves nothing",
  );

  spy.build(anchorIn("https://site.example/" + "b".repeat(1000000) + "/"), "a");
  assert.ok(
    spy.bases.every((length) => length <= 4096),
    `the parser was handed a base of ${Math.max(...spy.bases)} chars`,
  );
});

check("an over-cap base URI is not resolved against (M3b)", () => {
  // Resolving a short relative href against a page-controlled 1MB <base> builds a 1MB string per
  // element, before any cap on the RESULT can reject it. Bounding the base is what stops the work,
  // so a relative href degrades to opaque while an absolute one still resolves on its own.
  const hugeBaseDoc = {
    URL: "https://site.example/dir/page?q=1",
    baseURI: "https://site.example/" + "b".repeat(5000) + "/",
  };
  assert.deepStrictEqual(
    buildDestinationFacts(
      stubElement({ tag: "a", attrs: { href: "next" }, doc: hugeBaseDoc }),
      "a",
    ),
    { kind: "anchor", url: null },
  );
  assert.deepStrictEqual(
    buildDestinationFacts(
      stubElement({
        tag: "a",
        attrs: { href: "https://other.example/x" },
        doc: hugeBaseDoc,
      }),
      "a",
    ),
    { kind: "anchor", url: "https://other.example/x" },
  );
});

check(
  "a whitespace-only submitter override still overrides, like empty (L1)",
  () => {
    // Chromium strips leading/trailing C0-and-space before parsing a URL, so formaction="   " is
    // an EMPTY action: the DOCUMENT URL, not the owner form's action and not <base>. Resolving it
    // instead pointed the fact at a different ORIGIN than the browser's own submission.
    const form = stubForm({ action: "/owner-action", method: "post" });
    const facts = buildDestinationFacts(
      stubElement({
        tag: "input",
        attrs: { type: "submit", formaction: " \t\n ", formmethod: "  " },
        form,
      }),
      "input",
    );
    assert.deepStrictEqual(facts, {
      kind: "form",
      url: "https://site.example/dir/page?q=1",
      method: "get",
    });
  },
);

check("a present-but-empty submitter override still overrides (F6)", () => {
  // Chromium submits formaction="" to the DOCUMENT URL and normalizes formmethod="" to GET.
  // Treating empty as absent recorded the owner's action+method while the browser went
  // elsewhere — the invalid-button-type fail-open's sibling, falsified on the wire.
  const form = stubForm({ action: "/owner-action", method: "post" });
  const facts = buildDestinationFacts(
    stubElement({
      tag: "input",
      attrs: { type: "submit", formaction: "", formmethod: "" },
      form,
    }),
    "input",
  );
  assert.deepStrictEqual(facts, {
    kind: "form",
    url: "https://site.example/dir/page?q=1",
    method: "get",
  });
});

check(
  "the cap bounds the RESOLVED value, not just the raw attribute (F1)",
  () => {
    // 4000 raw chars of a compact non-ASCII character percent-encode to ~9x on resolution; a
    // raw-only cap admits them and the fact is duplicated into every owned control.
    const compact = "é".repeat(4000);
    const facts = buildDestinationFacts(
      stubElement({ tag: "a", attrs: { href: "/" + compact } }),
      "a",
    );
    assert.deepStrictEqual(facts, { kind: "anchor", url: null });
  },
);

check(
  "capture is flag-gated and budgeted at the attach site (F1/F4 source pin)",
  () => {
    // The budget's own behavior is covered below; this pins the two things only the attach site
    // can express — that capture is gated, and that an EXHAUSTED budget short-circuits before
    // buildDestinationFacts runs, which is what bounds resolution work and not just output.
    const attach = extract("buildElementObject");
    assert.ok(
      attach.includes(
        "if (__captureDestinationFacts && __destinationFactBudget > 0)",
      ),
      "capture flag gate or pre-build budget check deleted",
    );
    assert.ok(
      attach.includes("chargeDestinationBudget("),
      "budget charge deleted",
    );
    const build = extract("buildTreeFromBody");
    assert.ok(
      build.includes(
        "__captureDestinationFacts = captureDestinationFacts === true",
      ),
      "flag set deleted",
    );
    assert.ok(
      build.includes("__destinationFactBudget = 524288"),
      "budget init deleted",
    );
    assert.ok(
      build.includes("__captureDestinationFacts = false"),
      "flag reset deleted",
    );
  },
);

check("buildElementObject attaches the facts to the element object", () => {
  // The behavioral checks above drive buildDestinationFacts directly; this pins the production
  // attach site, so deleting the attachment cannot leave this suite green.
  const body = extract("buildElementObject");
  assert.ok(
    /buildDestinationFacts\(\s*element,\s*elementTagNameLower,?\s*\)/.test(
      body,
    ),
    "buildElementObject no longer computes destination facts",
  );
  assert.ok(
    body.includes("elementObj.destination = destinationFacts"),
    "buildElementObject no longer attaches destination facts",
  );
});

// The per-build byte budget shipped with only a source pin, so deleting it left the whole suite
// green while 15,000 elements produced 840,000 bytes of facts. These drive the real charge
// function, with the real overhead constant read out of the source.
const OVERHEAD = Number(
  /const DESTINATION_FACT_OVERHEAD = (\d+);/.exec(src)[1],
);
function budgetHarness(startingBudget) {
  return new Function(
    `let __destinationFactBudget = ${startingBudget};
     const DESTINATION_FACT_OVERHEAD = ${OVERHEAD};
     ${extract("spendDestinationBudget")}
     ${extract("chargeDestinationBudget")}
     return {
       charge: chargeDestinationBudget,
       spend: spendDestinationBudget,
       remaining: () => __destinationFactBudget,
     };`,
  )();
}

check(
  "the budget charges a fact's whole serialized cost, not just its url",
  () => {
    const budget = budgetHarness(10000);
    const facts = {
      kind: "form",
      url: "https://site.example/x",
      method: "get",
    };
    assert.strictEqual(budget.charge(facts), facts);
    assert.strictEqual(budget.remaining(), 10000 - OVERHEAD - facts.url.length);
  },
);

check("an OPAQUE fact still costs the per-fact overhead", () => {
  // url:null facts are not free: 15,000 of them serialized to 615,000 bytes while a url-only
  // charge said they cost nothing at all.
  const budget = budgetHarness(10000);
  const facts = { kind: "form", url: null, method: "get" };
  assert.strictEqual(budget.charge(facts), facts);
  assert.strictEqual(budget.remaining(), 10000 - OVERHEAD);
});

check(
  "an over-budget fact is DROPPED, and exhaustion zeroes the budget",
  () => {
    // Dropping (not url-nulling) is what bounds the payload, and zeroing is what makes the attach
    // site skip buildDestinationFacts entirely for every later element — resolution is the
    // expensive half and it runs before any per-URL cap can reject the result.
    const budget = budgetHarness(OVERHEAD + 5);
    assert.strictEqual(
      budget.charge({ kind: "anchor", url: "https://site.example/too-long" }),
      null,
    );
    assert.strictEqual(budget.remaining(), 0);
  },
);

check(
  "charging nothing is a no-op the caller can pass straight through",
  () => {
    const budget = budgetHarness(10000);
    assert.strictEqual(budget.charge(null), null);
    assert.strictEqual(budget.remaining(), 10000);
  },
);

check("a hostile cost cannot INFLATE the budget", () => {
  // _wrap_js_in_isolated_scope exports every top-level declaration onto globalThis, so a page can
  // call this while a build is awaiting. Probed in real Chromium: spend(-Infinity) returned true,
  // set the budget to Infinity, and 5,000 consecutive 4,000-char facts were then all accepted —
  // both the resolution bound and the payload bound gone. NaN is the same defect by another
  // route, since `x < NaN` is false, so the deduction runs and poisons every later comparison.
  // The pre-existing entry point could not reach this: a fact's cost is a string length.
  for (const hostile of [
    -Infinity,
    NaN,
    -1,
    -0.5,
    "5",
    null,
    undefined,
    {},
    1e309,
  ]) {
    const label = String(hostile);
    const budget = budgetHarness(10000);
    assert.strictEqual(
      budget.spend(hostile),
      false,
      `accepted a cost of ${label}`,
    );
    assert.strictEqual(
      budget.remaining(),
      0,
      `budget not zeroed after ${label}`,
    );
    assert.strictEqual(
      budget.charge({ kind: "anchor", url: "z" }),
      null,
      `still spendable after ${label}`,
    );
  }
  // Non-vacuity: an ordinary cost must still be accepted, or the guard above proves nothing.
  const ok = budgetHarness(10000);
  assert.strictEqual(ok.spend(100), true);
  assert.strictEqual(ok.remaining(), 9900);
});

// The two checks below drive a whole build's worth of elements through the real builder and the
// real charge functions, with a URL constructor that counts what resolution actually allocates.
// Both were unreachable while the suite only exercised single elements: a per-element assertion
// cannot see a per-build bound.
const PER_BUILD_BUDGET = Math.max(
  ...[...src.matchAll(/__destinationFactBudget = (\d+);/g)].map((match) =>
    Number(match[1]),
  ),
);
const MAX_ELEMENTS = Number(/const maxElementNumber = (\d+);/.exec(src)[1]);

function buildLoop(href, elementCount) {
  let urlConstructions = 0;
  let resolvedChars = 0;
  class CountingURL extends URL {
    constructor(...args) {
      super(...args);
      urlConstructions++;
      resolvedChars += this.href.length;
    }
  }
  const harness = new Function(
    "URL",
    `let __destinationFactBudget = ${PER_BUILD_BUDGET};
     const DESTINATION_FACT_OVERHEAD = ${OVERHEAD};
     ${extract("normalizeFormMethod")}
     ${extract("spendDestinationBudget")}
     ${extract("chargeDestinationBudget")}
     ${extract("buildDestinationFacts")}
     return { build: buildDestinationFacts, charge: chargeDestinationBudget,
              remaining: () => __destinationFactBudget };`,
  )(CountingURL);
  let attached = 0;
  for (let i = 0; i < elementCount; i++) {
    // The buildElementObject attach-site guard: exhaustion must stop the work, not just the bytes.
    if (harness.remaining() > 0) {
      if (
        harness.charge(
          harness.build(stubElement({ tag: "a", attrs: { href } }), "a"),
        )
      ) {
        attached++;
      }
    }
  }
  return {
    urlConstructions,
    resolvedChars,
    attached,
    remaining: harness.remaining(),
  };
}

check(
  "the budget bounds RESOLUTION WORK, not just the bytes it attaches",
  () => {
    // A raw href OVER the per-URL cap is the worst case and the cheapest to charge: it resolves to
    // ~9x its length, is rejected by the cap, and then bills 56 bytes as an opaque fact. Charging
    // only the surviving fact let 15,000 elements buy 9,363 resolutions and 329MiB of allocated
    // href for a 512KiB budget. Charging the resolution's input caps a build's total work at
    // 9 x budget by construction. Deleting the pre-resolution charge makes this red.
    const hostile = "€".repeat(4096); // 3-byte UTF-8: percent-encodes 1 char -> 9.
    const run = buildLoop(hostile, MAX_ELEMENTS);
    assert.ok(
      run.urlConstructions < 500,
      `resolution unbounded: ${run.urlConstructions} URLs built from ${MAX_ELEMENTS} elements`,
    );
    assert.ok(
      run.resolvedChars <= PER_BUILD_BUDGET * 9,
      `resolved ${run.resolvedChars} chars, above the 9 x budget bound`,
    );
    assert.strictEqual(run.remaining, 0);
  },
);

check(
  "the per-build budget stops a build of individually under-cap facts",
  () => {
    // The budget's own regression test. Every fact here is UNDER the per-URL cap, so none degrade
    // to opaque and each bills its full serialized length — which is the only shape where the
    // per-build budget is the binding constraint. The pre-existing hostile string resolved OVER the
    // cap, so every fact was already opaque and deleting the budget left the suite green.
    const underCap = "€".repeat(440); // resolves to ~3,985 chars: under the 4096 cap.
    const run = buildLoop(underCap, MAX_ELEMENTS);
    assert.ok(
      run.attached > 0 && run.attached < MAX_ELEMENTS,
      `budget did not bind: ${run.attached} of ${MAX_ELEMENTS} attached`,
    );
    assert.ok(
      run.resolvedChars <= PER_BUILD_BUDGET * 9,
      `resolved ${run.resolvedChars} chars, above the 9 x budget bound`,
    );
    assert.strictEqual(run.remaining, 0);
  },
);

check("a benign page still gets facts on thousands of elements", () => {
  // The bound must cost hostile pages, not ordinary ones: charging the input is a real reduction
  // in benign capacity, so pin that it stays far above any realistic destination-bearing count.
  const run = buildLoop("next/page", MAX_ELEMENTS);
  assert.ok(
    run.attached > 3000,
    `benign capacity collapsed to ${run.attached}`,
  );
  assert.deepStrictEqual(
    buildDestinationFacts(
      stubElement({ tag: "a", attrs: { href: "next/page" } }),
      "a",
    ),
    { kind: "anchor", url: "https://site.example/dir/next/page" },
  );
});

if (failures.length > 0) {
  console.error(`${failures.length} failure(s): ${failures.join(", ")}`);
  process.exit(1);
}
console.log("all destination-fact checks passed");
