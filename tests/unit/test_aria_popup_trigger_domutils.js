/**
 * Behavioral tests for getOpenAriaPopupTrigger() in domUtils.js.
 *
 * The predicate reports an open ARIA popup trigger (combobox / haspopup) so the
 * screenshot-scroll policy can skip scrolling for one capture and avoid dismissing
 * a just-opened portal popup (e.g. a date-picker overlay or ARIA listbox).
 *
 * Exit 0 = pass, exit 1 = failures on stderr.
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(
  path.join(__dirname, "../../skyvern/webeye/scraper/domUtils.js"),
  "utf8",
);

// Node-scope DOM registries the mocked document closures read.
let allElements = [];
const byId = new Map();

function reset() {
  allElements = [];
  byId.clear();
}

function getComputedStyle(element) {
  return {
    display: "block",
    visibility: "visible",
    opacity: "1",
    cursor: "auto",
    pointerEvents: "auto",
    ...(element._style || {}),
  };
}

class TestElement {}

const context = {
  console,
  window: {
    scrollX: 0,
    scrollY: 0,
    innerHeight: 800,
    innerWidth: 1200,
    getComputedStyle,
  },
  document: {
    documentElement: {},
    querySelector() {
      return null;
    },
    querySelectorAll(selector) {
      if (selector === '[aria-expanded="true"]') {
        return allElements.filter(
          (el) => el.getAttribute("aria-expanded") === "true",
        );
      }
      return [];
    },
    getElementById(id) {
      return byId.get(id) || null;
    },
  },
  MutationObserver: class {
    observe() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  },
  Element: TestElement,
  ShadowRoot: class {},
  Node: { ELEMENT_NODE: 1, TEXT_NODE: 3 },
  Number,
  Set,
};
context.global = context;
vm.runInNewContext(
  `${src}
this.__exports = { getOpenAriaPopupTrigger };`,
  context,
);

function el(opts) {
  const element = new TestElement();
  element.tagName = (opts.tagName || "DIV").toUpperCase();
  element.nodeName = element.tagName;
  element.hidden = Boolean(opts.hidden);
  element.disabled = Boolean(opts.disabled);
  element._style = opts.style || {};
  const rect = opts.rect || { width: 120, height: 32, left: 40, top: 120 };
  element._rect = {
    width: rect.width,
    height: rect.height,
    left: rect.left,
    top: rect.top,
    right: rect.right !== undefined ? rect.right : rect.left + rect.width,
    bottom: rect.bottom !== undefined ? rect.bottom : rect.top + rect.height,
  };
  element._attributes = { ...(opts.attributes || {}) };
  if (opts.id) {
    element._attributes.id = opts.id;
  }
  element.getAttribute = function (name) {
    return name in this._attributes ? this._attributes[name] : null;
  };
  element.hasAttribute = function (name) {
    return name in this._attributes;
  };
  element.getBoundingClientRect = function () {
    return this._rect;
  };
  element.ownerDocument = { defaultView: { getComputedStyle } };
  element.getRootNode = function () {
    return { host: null };
  };
  element.parentElement = opts.parent || null;
  element.children = opts.children || [];
  for (const child of element.children) {
    child.parentElement = element;
  }
  element.closest = function () {
    return null;
  };
  allElements.push(element);
  if (opts.id) {
    byId.set(opts.id, element);
  }
  return element;
}

// Build a chain of nested ancestors: a top ancestor styled by `topStyle`, then `levels` visible
// descendants below it; returns the bottom-most node (use it as a deep element's parent).
function deepChain(topStyle, levels) {
  let node = el({ tagName: "DIV", style: topStyle });
  for (let i = 0; i < levels; i++) {
    node = el({ tagName: "DIV", parent: node });
  }
  return node;
}

let passed = 0;
let failed = 0;

function test(name, fn) {
  reset();
  try {
    fn();
    passed++;
    process.stdout.write(`  PASS: ${name}\n`);
  } catch (e) {
    failed++;
    process.stderr.write(`  FAIL: ${name}\n    ${e.message}\n`);
  }
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

const trigger = () => context.__exports.getOpenAriaPopupTrigger();

test("open combobox with no aria-controls is reported (unresolved => open)", () => {
  el({
    tagName: "DIV",
    attributes: { role: "combobox", "aria-expanded": "true" },
  });
  const result = trigger();
  assert(
    result !== null,
    "an open combobox with no controls should be detected",
  );
  assert(
    result.role === "combobox",
    `role should be combobox, got ${result && result.role}`,
  );
});

test("closed combobox (aria-expanded=false) is not reported", () => {
  el({
    tagName: "DIV",
    attributes: { role: "combobox", "aria-expanded": "false" },
  });
  assert(trigger() === null, "a closed combobox must not be reported");
});

test("aria-haspopup=listbox with a visible controlled target is reported", () => {
  el({
    tagName: "DIV",
    attributes: {
      "aria-haspopup": "listbox",
      "aria-expanded": "true",
      "aria-controls": "lb1",
    },
  });
  el({ id: "lb1", tagName: "UL", attributes: { role: "listbox" } });
  const result = trigger();
  assert(
    result !== null,
    "haspopup=listbox with a visible target should be detected",
  );
  assert(
    result.hasPopup === "listbox",
    `hasPopup should be listbox, got ${result && result.hasPopup}`,
  );
  assert(
    result.controlsResolved === 1,
    `controlsResolved should be 1, got ${result && result.controlsResolved}`,
  );
});

test("menu trigger whose only controlled target is display:none is closed", () => {
  el({
    tagName: "BUTTON",
    attributes: {
      "aria-haspopup": "menu",
      "aria-expanded": "true",
      "aria-controls": "m1",
    },
  });
  el({ id: "m1", tagName: "DIV", style: { display: "none" } });
  assert(
    trigger() === null,
    "all resolved targets hidden => closed => not reported",
  );
});

test("listbox trigger with one hidden and one visible target is open (any visible => open)", () => {
  el({
    tagName: "DIV",
    attributes: {
      "aria-haspopup": "listbox",
      "aria-expanded": "true",
      "aria-controls": "hidden1 visible1",
    },
  });
  el({ id: "hidden1", tagName: "DIV", style: { display: "none" } });
  el({ id: "visible1", tagName: "UL" });
  const result = trigger();
  assert(result !== null, "any visible resolved target => open");
  assert(
    result.controlsResolved === 2,
    `controlsResolved should be 2, got ${result && result.controlsResolved}`,
  );
});

test("aria-owns is parsed like aria-controls", () => {
  el({
    tagName: "DIV",
    attributes: {
      role: "combobox",
      "aria-expanded": "true",
      "aria-owns": "own1",
    },
  });
  el({ id: "own1", tagName: "DIV", style: { display: "none" } });
  assert(trigger() === null, "aria-owns target hidden => closed");
});

test("expanded element with no combobox role and no popup role does not qualify", () => {
  el({ tagName: "BUTTON", attributes: { "aria-expanded": "true" } });
  assert(
    trigger() === null,
    "a bare aria-expanded element (accordion) must not qualify",
  );
});

test("open combobox scrolled fully off-screen is not reported", () => {
  el({
    tagName: "DIV",
    attributes: { role: "combobox", "aria-expanded": "true" },
    rect: { width: 120, height: 32, left: 40, top: 2000 },
  });
  assert(
    trigger() === null,
    "a trigger below the viewport must not be reported",
  );
});

test("open combobox with display:none is not reported", () => {
  el({
    tagName: "DIV",
    attributes: { role: "combobox", "aria-expanded": "true" },
    style: { display: "none" },
  });
  assert(trigger() === null, "a display:none trigger must not be reported");
});

test("aria-haspopup=true (string) qualifies", () => {
  el({
    tagName: "DIV",
    attributes: { "aria-haspopup": "true", "aria-expanded": "true" },
  });
  assert(trigger() !== null, "aria-haspopup='true' should qualify");
});

test("aria-haspopup=dialog with grid/tree values all qualify", () => {
  for (const value of ["dialog", "grid", "tree"]) {
    reset();
    el({
      tagName: "DIV",
      attributes: { "aria-haspopup": value, "aria-expanded": "true" },
    });
    assert(trigger() !== null, `aria-haspopup='${value}' should qualify`);
  }
});

test("multiple whitespace-separated IDREFs none of which resolve => closed (all dangling)", () => {
  el({
    tagName: "DIV",
    attributes: {
      role: "combobox",
      "aria-expanded": "true",
      "aria-controls": "gone1   gone2\tgone3",
    },
  });
  // The trigger named IDREFs but none resolve: the popup was unmounted on close, so it is closed —
  // distinct from a trigger with no aria-controls at all (which keeps the portal fallback => open).
  assert(
    trigger() === null,
    "present-but-all-dangling IDREFs must be treated as closed, not open",
  );
});

test("target inside a display:none ancestor is closed (ancestor-aware)", () => {
  const hiddenAncestor = el({ tagName: "DIV", style: { display: "none" } });
  el({
    tagName: "BUTTON",
    attributes: {
      "aria-haspopup": "menu",
      "aria-expanded": "true",
      "aria-controls": "t1",
    },
  });
  el({ id: "t1", tagName: "DIV", parent: hiddenAncestor });
  assert(
    trigger() === null,
    "a target under a display:none ancestor must not count as open",
  );
});

test("target inside a visibility:hidden ancestor is closed (ancestor-aware)", () => {
  const hiddenAncestor = el({
    tagName: "DIV",
    style: { visibility: "hidden" },
  });
  el({
    tagName: "BUTTON",
    attributes: {
      "aria-haspopup": "listbox",
      "aria-expanded": "true",
      "aria-controls": "t2",
    },
  });
  el({ id: "t2", tagName: "UL", parent: hiddenAncestor });
  assert(
    trigger() === null,
    "a target under a visibility:hidden ancestor must not count as open",
  );
});

test("target inside an opacity:0 ancestor is closed (ancestor-aware)", () => {
  const hiddenAncestor = el({ tagName: "DIV", style: { opacity: "0" } });
  el({
    tagName: "BUTTON",
    attributes: {
      "aria-haspopup": "dialog",
      "aria-expanded": "true",
      "aria-controls": "t3",
    },
  });
  el({ id: "t3", tagName: "DIV", parent: hiddenAncestor });
  assert(
    trigger() === null,
    "a target under an opacity:0 ancestor must not count as open",
  );
});

test("trigger inside a display:none ancestor is not detected (ancestor-aware)", () => {
  const hiddenAncestor = el({ tagName: "DIV", style: { display: "none" } });
  el({
    tagName: "DIV",
    attributes: { role: "combobox", "aria-expanded": "true" },
    parent: hiddenAncestor,
  });
  assert(
    trigger() === null,
    "a trigger under a display:none ancestor must not be reported",
  );
});

test("faithful visible portal under a visible ancestor is still open (control)", () => {
  const visibleAncestor = el({
    tagName: "DIV",
    style: { display: "block", opacity: "1" },
  });
  el({
    tagName: "BUTTON",
    attributes: {
      "aria-haspopup": "listbox",
      "aria-expanded": "true",
      "aria-controls": "t4",
    },
  });
  el({ id: "t4", tagName: "UL", parent: visibleAncestor });
  const result = trigger();
  assert(
    result !== null,
    "a target under a visible ancestor must still count as open",
  );
  assert(
    result.controlsResolved === 1,
    `controlsResolved should be 1, got ${result && result.controlsResolved}`,
  );
});

test("target nested >50 levels under a display:none ancestor is closed (full walk to root)", () => {
  const bottom = deepChain({ display: "none" }, 55);
  el({
    tagName: "BUTTON",
    attributes: {
      "aria-haspopup": "menu",
      "aria-expanded": "true",
      "aria-controls": "deepT",
    },
  });
  el({ id: "deepT", tagName: "DIV", parent: bottom });
  assert(
    trigger() === null,
    "a target under a display:none ancestor >50 levels up must not count as open",
  );
});

test("trigger nested >50 levels under a display:none ancestor is not reported (full walk to root)", () => {
  const bottom = deepChain({ display: "none" }, 55);
  el({
    tagName: "DIV",
    attributes: { role: "combobox", "aria-expanded": "true" },
    parent: bottom,
  });
  assert(
    trigger() === null,
    "a trigger under a display:none ancestor >50 levels up must not be reported",
  );
});

test("valid deep portal (>50 levels, all visible) is still open (control — walk must not over-reject)", () => {
  const bottom = deepChain({ display: "block", opacity: "1" }, 55);
  el({
    tagName: "BUTTON",
    attributes: {
      "aria-haspopup": "listbox",
      "aria-expanded": "true",
      "aria-controls": "deepV",
    },
  });
  el({ id: "deepV", tagName: "UL", parent: bottom });
  const result = trigger();
  assert(
    result !== null,
    "a deep portal with all-visible ancestors must still count as open",
  );
  assert(
    result.controlsResolved === 1,
    `controlsResolved should be 1, got ${result && result.controlsResolved}`,
  );
});

test("target under a content-visibility:hidden ancestor is closed", () => {
  const hiddenAncestor = el({
    tagName: "DIV",
    style: { contentVisibility: "hidden" },
  });
  el({
    tagName: "BUTTON",
    attributes: {
      "aria-haspopup": "menu",
      "aria-expanded": "true",
      "aria-controls": "cv1",
    },
  });
  el({ id: "cv1", tagName: "DIV", parent: hiddenAncestor });
  assert(
    trigger() === null,
    "a target under a content-visibility:hidden ancestor must not count as open",
  );
});

test("trigger under a content-visibility:hidden ancestor is not reported", () => {
  const hiddenAncestor = el({
    tagName: "DIV",
    style: { contentVisibility: "hidden" },
  });
  el({
    tagName: "DIV",
    attributes: { role: "combobox", "aria-expanded": "true" },
    parent: hiddenAncestor,
  });
  assert(
    trigger() === null,
    "a trigger under a content-visibility:hidden ancestor must not be reported",
  );
});

test("target with its own content-visibility:hidden is closed", () => {
  el({
    tagName: "BUTTON",
    attributes: {
      "aria-haspopup": "listbox",
      "aria-expanded": "true",
      "aria-controls": "cv2",
    },
  });
  el({ id: "cv2", tagName: "UL", style: { contentVisibility: "hidden" } });
  assert(
    trigger() === null,
    "a target with its own content-visibility:hidden must not count as open",
  );
});

test("content-visibility:visible ancestor does not reject a valid portal (control)", () => {
  const visibleAncestor = el({
    tagName: "DIV",
    style: { contentVisibility: "visible" },
  });
  el({
    tagName: "BUTTON",
    attributes: {
      "aria-haspopup": "listbox",
      "aria-expanded": "true",
      "aria-controls": "cv3",
    },
  });
  el({ id: "cv3", tagName: "UL", parent: visibleAncestor });
  assert(
    trigger() !== null,
    "a content-visibility:visible ancestor must not reject a valid portal",
  );
});

// --- Rendered-area gate for the controlled target ----------------------------------------------
// checkVisibility() / style visibility pass for a zero-area or transform:scale(0) target, so a
// stale aria-expanded=true trigger pointing at a target that paints nothing would false-positive as
// open and suppress a needed scroll under treatment. The target must actually render an area.

test("zero-area controlled target is closed (rendered-area gate)", () => {
  el({
    tagName: "BUTTON",
    attributes: {
      "aria-haspopup": "listbox",
      "aria-expanded": "true",
      "aria-controls": "za1",
    },
  });
  el({
    id: "za1",
    tagName: "UL",
    rect: { width: 0, height: 0, left: 40, top: 120 },
  });
  assert(
    trigger() === null,
    "a zero-area controlled target must not count as open",
  );
});

test("transform:scale(0) controlled target is closed (rendered-area gate)", () => {
  el({
    tagName: "BUTTON",
    attributes: {
      "aria-haspopup": "listbox",
      "aria-expanded": "true",
      "aria-controls": "sc1",
    },
  });
  // scale(0) collapses the box AND its subtree to zero on-screen area.
  const scaledChild = el({
    tagName: "LI",
    rect: { width: 0, height: 0, left: 40, top: 120 },
  });
  el({
    id: "sc1",
    tagName: "UL",
    style: { transform: "scale(0)" },
    rect: { width: 0, height: 0, left: 40, top: 120 },
    children: [scaledChild],
  });
  assert(
    trigger() === null,
    "a transform:scale(0) controlled target must not count as open",
  );
});

test("zero-size overflow:hidden target with a positive child is closed (clipped)", () => {
  el({
    tagName: "BUTTON",
    attributes: {
      "aria-haspopup": "listbox",
      "aria-expanded": "true",
      "aria-controls": "oh1",
    },
  });
  // The child has layout area but is clipped to the 0x0 content box, so nothing paints.
  const clippedChild = el({
    tagName: "LI",
    rect: { width: 240, height: 120, left: 40, top: 120 },
  });
  el({
    id: "oh1",
    tagName: "UL",
    style: { overflow: "hidden", overflowX: "hidden", overflowY: "hidden" },
    rect: { width: 0, height: 0, left: 40, top: 120 },
    children: [clippedChild],
  });
  assert(
    trigger() === null,
    "a zero-size overflow:hidden container clips its child => closed",
  );
});

test("zero-size overflow:visible portal root with a rendered visible descendant is open", () => {
  el({
    tagName: "BUTTON",
    attributes: {
      "aria-haspopup": "listbox",
      "aria-expanded": "true",
      "aria-controls": "ov1",
    },
  });
  // A portal mount point that itself has no box, but a genuinely painted panel escapes it.
  const panel = el({
    tagName: "DIV",
    rect: { width: 240, height: 120, left: 40, top: 120 },
  });
  el({
    id: "ov1",
    tagName: "DIV",
    style: { overflow: "visible", overflowX: "visible", overflowY: "visible" },
    rect: { width: 0, height: 0, left: 40, top: 120 },
    children: [panel],
  });
  const result = trigger();
  assert(
    result !== null,
    "an overflow:visible zero-area portal root with a rendered descendant => open",
  );
  assert(
    result.controlsResolved === 1,
    `controlsResolved should be 1, got ${result && result.controlsResolved}`,
  );
});

test("positive rendered controlled target is open (rendered-area gate control)", () => {
  el({
    tagName: "BUTTON",
    attributes: {
      "aria-haspopup": "listbox",
      "aria-expanded": "true",
      "aria-controls": "pr1",
    },
  });
  el({
    id: "pr1",
    tagName: "UL",
    rect: { width: 240, height: 120, left: 40, top: 120 },
  });
  const result = trigger();
  assert(
    result !== null,
    "a positive-area controlled target must count as open",
  );
  assert(
    result.controlsResolved === 1,
    `controlsResolved should be 1, got ${result && result.controlsResolved}`,
  );
});

// --- dangling vs absent aria-controls (a present-but-unresolved IDREF is a popup unmounted on
// close, not a portal wired without aria-controls) ---

test("dangling aria-controls (present IDREF, target unmounted) is closed", () => {
  el({
    tagName: "BUTTON",
    attributes: {
      "aria-haspopup": "listbox",
      "aria-expanded": "true",
      "aria-controls": "ghost1",
    },
  });
  // no element with id ghost1 exists (unmounted on close)
  assert(
    trigger() === null,
    "a present-but-dangling aria-controls must be treated as closed, not open",
  );
});

test("dangling aria-owns (present IDREF, target unmounted) is closed", () => {
  el({
    tagName: "DIV",
    attributes: {
      role: "combobox",
      "aria-expanded": "true",
      "aria-owns": "ghostOwned",
    },
  });
  assert(
    trigger() === null,
    "a present-but-dangling aria-owns must be treated as closed",
  );
});

test("absent aria-controls keeps portal fallback => open (controlsResolved=0)", () => {
  el({
    tagName: "BUTTON",
    attributes: { "aria-haspopup": "listbox", "aria-expanded": "true" },
  });
  const result = trigger();
  assert(result !== null, "absent aria-controls => portal fallback => open");
  assert(
    result.controlsResolved === 0,
    `controlsResolved should be 0 for the no-target fallback, got ${result && result.controlsResolved}`,
  );
});

test("empty/whitespace aria-controls is treated as absent => open", () => {
  el({
    tagName: "DIV",
    attributes: {
      role: "combobox",
      "aria-expanded": "true",
      "aria-controls": "   ",
    },
  });
  assert(
    trigger() !== null,
    "a whitespace-only aria-controls references no IDREF and must fall back to portal-open",
  );
});

test("mixed dangling + resolved-visible target is open (any resolved-visible => open)", () => {
  el({
    tagName: "DIV",
    attributes: {
      "aria-haspopup": "listbox",
      "aria-expanded": "true",
      "aria-controls": "ghostX visibleX",
    },
  });
  el({ id: "visibleX", tagName: "UL" });
  const result = trigger();
  assert(result !== null, "a resolved-visible target among danglers => open");
  assert(
    result.controlsResolved === 1,
    `controlsResolved should count only resolved targets (1), got ${result && result.controlsResolved}`,
  );
});

// --- telemetry token bounding: role / aria-haspopup are page-controlled and must not reach the
// return (and thus telemetry) verbatim; only the matched allowlisted token is emitted ---

test("haspopup-qualified trigger with arbitrary role emits null role, matched hasPopup", () => {
  el({
    tagName: "BUTTON",
    attributes: {
      role: "'\"><script>arbitrary-attacker-role",
      "aria-haspopup": "menu",
      "aria-expanded": "true",
    },
  });
  const result = trigger();
  assert(result !== null, "haspopup=menu qualifies");
  assert(
    result.role === null,
    `arbitrary role must not be emitted, got ${result && JSON.stringify(result.role)}`,
  );
  assert(
    result.hasPopup === "menu",
    `matched hasPopup token must be emitted, got ${result && result.hasPopup}`,
  );
});

test("combobox-qualified trigger with arbitrary aria-haspopup emits null hasPopup, matched role", () => {
  el({
    tagName: "DIV",
    attributes: {
      role: "combobox",
      "aria-haspopup": "arbitrary-attacker-haspopup-value",
      "aria-expanded": "true",
    },
  });
  const result = trigger();
  assert(result !== null, "role=combobox qualifies");
  assert(
    result.role === "combobox",
    `matched role token must be emitted, got ${result && result.role}`,
  );
  assert(
    result.hasPopup === null,
    `non-allowlisted aria-haspopup must not be emitted, got ${result && JSON.stringify(result.hasPopup)}`,
  );
});

test("both matched tokens are emitted (combobox + menu)", () => {
  el({
    tagName: "DIV",
    attributes: {
      role: "combobox",
      "aria-haspopup": "menu",
      "aria-expanded": "true",
    },
  });
  const result = trigger();
  assert(result !== null, "combobox + menu qualifies");
  assert(
    result.role === "combobox",
    `role should be combobox, got ${result && result.role}`,
  );
  assert(
    result.hasPopup === "menu",
    `hasPopup should be menu, got ${result && result.hasPopup}`,
  );
});

console.log(`\n${passed + failed} tests: ${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
