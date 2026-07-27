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

const buildDestinationFacts = new Function(
  `${extract("normalizeFormMethod")}\n${extract("buildDestinationFacts")}\nreturn buildDestinationFacts;`,
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
  const spy = new Function(
    `const bases = [];
     class URL extends globalThis.URL {
       constructor(raw, base) {
         bases.push(base === undefined ? -1 : String(base).length);
         super(raw, base);
       }
     }
     ${extract("normalizeFormMethod")}
     ${extract("buildDestinationFacts")}
     return { build: buildDestinationFacts, bases };`,
  )();
  const hugeBase = "https://site.example/" + "b".repeat(1000000) + "/";
  spy.build(
    {
      tagName: "a",
      form: undefined,
      ownerDocument: {
        URL: "https://site.example/dir/page",
        baseURI: hugeBase,
      },
      getAttribute: (name) => (name === "href" ? "next" : null),
    },
    "a",
  );
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
     ${extract("chargeDestinationBudget")}
     return {
       charge: chargeDestinationBudget,
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

if (failures.length > 0) {
  console.error(`${failures.length} failure(s): ${failures.join(", ")}`);
  process.exit(1);
}
console.log("all destination-fact checks passed");
