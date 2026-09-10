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

// Exercise the actual serializer, including copied attributes and the live value property.
(async () => {
  const vm = require("vm");
  const context = {
    window: { GlobalSkyvernFrameIndex: 0 },
    ShadowRoot: global.ShadowRoot,
    __captureDestinationFacts: false,
    getPseudoContent: () => "",
    getElementText: () => "",
    enrichValidationState: () => {},
  };
  for (const name of [
    "checkDisabledFromStyle",
    "checkRequiredFromStyle",
    "isHoverOnlyElement",
    "isDatePickerSelector",
    "isDivComboboxDropdown",
    "isDropdownButton",
    "isAngularDropdown",
    "isAngularMaterialDatePicker",
    "isSelect2Dropdown",
    "isSelect2MultiChoice",
    "isReadonlyInputDropdown",
  ])
    context[name] = () => false;
  vm.createContext(context);
  const helper = src.includes("// BEGIN OTP INPUT PRIVACY")
    ? src
        .split("// BEGIN OTP INPUT PRIVACY")[1]
        .split("// END OTP INPUT PRIVACY")[0]
    : extract("isOtpInputValueSecret");
  vm.runInContext(helper + "\nasync " + extract("buildElementObject"), context);
  for (const [name, attributes, value, count, flagged, expected] of [
    ["marked", { "data-skyvern-otp-box": "1" }, "7", 0, false, "*"],
    ["unmarked alone", {}, "8", 0, false, "8"],
    ["flagged maxlength", { maxlength: "1" }, "9", 6, true, "*"],
    ["flagged numeric", { inputmode: "numeric" }, "6", 6, true, "*"],
    ["flagged decimal", { inputmode: "decimal" }, "5", 7, true, "*"],
    ["flagged pattern", { pattern: "[0-9]" }, "4", 6, true, "*"],
    ["flagged digit pattern", { pattern: "\\d" }, "3", 6, true, "*"],
    ["unmarked widget", { maxlength: "1" }, "9", 6, true, "*"],
    [
      "invalid explicit length",
      { maxlength: "1.0", inputmode: "numeric" },
      "3",
      6,
      true,
      "3",
    ],
    ["whole code in box", { maxlength: "1" }, "654321", 6, true, "******"],
    ["lone quantity", { inputmode: "numeric" }, "3", 1, true, "3"],
    ["too few boxes", { maxlength: "1" }, "3", 5, true, "3"],
    [
      "email in form",
      { type: "email" },
      "a@example.com",
      6,
      true,
      "a@example.com",
    ],
    ["username in form", {}, "1234", 6, true, "1234"],
    [
      "unrelated numeric",
      { maxlength: "32", inputmode: "numeric" },
      "2",
      6,
      true,
      "2",
    ],
    ["empty", { "data-skyvern-otp-box": "1", maxlength: "1" }, "", 0, true, ""],
    ["new document", { maxlength: "1" }, "1", 6, false, "1"],
    ["password", { type: "password" }, "secret", 0, false, "******"],
  ]) {
    const input = el({
      tagName: "input",
      attributes: {
        unique_id: name,
        type: "text",
        value,
        "aria-valuenow": value,
        "aria-valuetext": value,
        "data-value": value,
        defaultvalue: value,
        placeholder: value,
        ...attributes,
      },
    });
    input.value = value;
    input.type = input.getAttribute("type");
    input.attributes = Object.entries(input._attributes).map(
      ([name, value]) => ({ name, value }),
    );
    input.setAttribute = (name, value) => {
      input._attributes[name] = value;
    };
    const form = el({
      tagName: name === "unmarked widget" ? "div" : "form",
    });
    const peers = Array.from({ length: count }, () =>
      el({ tagName: "input", type: "text", attributes: { maxlength: "1" } }),
    );
    form.children = peers;
    form.querySelectorAll = (selector) => (selector === "input" ? peers : []);
    input.parentElement = form;
    input.closest = (selector) => (selector === "form" ? form : null);
    input.ownerDocument = {
      documentElement: {
        hasAttribute: () => flagged,
        getAttribute: () => (flagged ? "6" : null),
      },
    };
    const serialized = await context.buildElementObject(
      "main.frame",
      input,
      true,
    );
    test(`OTP serializer: ${name}`, () => {
      assert(
        serialized.attributes.value === expected,
        `${name}: incorrect serialized value`,
      );
      for (const attr of [
        "aria-valuenow",
        "aria-valuetext",
        "data-value",
        "defaultvalue",
        "placeholder",
      ]) {
        assert(
          serialized.attributes[attr] === expected,
          `${name}: ${attr} was not handled with value`,
        );
      }
      assert(
        input.value === value && input._attributes.value === value,
        "serialization changed the live input",
      );
    });
  }
  test("OTP stamp pass covers hidden light and shadow inputs", () => {
    const root = el({
      tagName: "html",
      attributes: { "data-skyvern-otp-filled": "6" },
    });
    const form = el({ tagName: "form", parentElement: root });
    const host = el({ parentElement: form });
    const ownerDocument = { documentElement: root };
    const inputs = Array.from({ length: 6 }, (_, index) => {
      const input = el({
        tagName: "input",
        attributes: { maxlength: "1", value: String(index) },
        style: { display: "none" },
      });
      input.ownerDocument = ownerDocument;
      input.parentElement = index < 2 ? form : null;
      input.getRootNode = () => ({ host });
      input.setAttribute = (name, value) => {
        input._attributes[name] = value;
      };
      return input;
    });
    root.children = [form];
    form.children = [inputs[0], inputs[1], host];
    host.shadowRoot = { children: inputs.slice(2) };
    context.document = ownerDocument;
    context.stampOtpInputBoxes();
    assert(
      inputs.every(
        (input) => input.getAttribute("data-skyvern-otp-box") === "1",
      ),
      "hidden box was not stamped",
    );
    let visited = false;
    context.document = {
      documentElement: {
        hasAttribute: () => false,
        get children() {
          visited = true;
          return [];
        },
      },
    };
    context.stampOtpInputBoxes();
    assert(!visited, "unarmed document must not be traversed");
  });
  test("OTP placeholder without matching value attribute", () => {
    for (const value of [null, "", "0"]) {
      const input = el({
        tagName: "input",
        attributes: value === null ? {} : { value },
      });
      input.value = "6";
      for (const [placeholder, expected] of [
        ["6", "*"],
        ["654321", "******"],
        ["Digit 1", "Digit 1"],
        ["Code", "Code"],
      ])
        assert(
          context.otpSafeInputAttribute(input, "placeholder", placeholder) ===
            expected,
          "placeholder mismatch",
        );
    }
  });
  test("OTP large form reuses container counts", () => {
    let visits = 0;
    const form = el({ tagName: "form" });
    const ownerDocument = { documentElement: { getAttribute: () => "6" } };
    const inputs = Array.from({ length: 1000 }, () => {
      const input = el({
        tagName: "input",
        attributes: { inputmode: "numeric" },
        parentElement: form,
      });
      input.ownerDocument = ownerDocument;
      return input;
    });
    const nodes = [...inputs, ...Array.from({ length: 10000 }, () => el({}))];
    for (const node of nodes) {
      const tagName = node.tagName;
      Object.defineProperty(node, "tagName", {
        get() {
          visits++;
          return tagName;
        },
      });
    }
    form.children = nodes;
    assert(
      inputs.every((input) => context.isOtpInputValueSecret(input)),
      "large-form boxes must be protected",
    );
    assert(visits < 100000, `too many node visits: ${visits}`);
    process.stdout.write(`    large-form tag reads: ${visits}\n`);
  });
  Object.assign(context, {
    getChildElements: (node) => node.children || [],
    isElementVisible: () => true,
    isHidden: () => false,
    isScriptOrStyle: () => false,
    isInteractable: (node) => node.tagName === "INPUT",
    isTableRelatedElement: () => false,
    hasBeforeOrAfterPseudoContent: () => false,
    isDOMNodeRepresentDiv: () => false,
    waitForNextFrame: async () => {},
    asyncSleepFor: async () => {},
    _jsConsoleError: (message) => {
      throw new Error(message);
    },
  });
  vm.runInContext(
    extract("getElementDomDepth") +
      "\n" +
      ["buildElementTree", "addIncrementalNodeToMap", "getIncrementElements"]
        .map((name) => "async " + extract(name))
        .join("\n"),
    context,
  );
  for (const [nested, replaced] of [
    [false, false],
    [true, false],
    [false, true],
    [true, true],
  ]) {
    const root = el({
      tagName: "html",
      attributes: { "data-skyvern-otp-filled": "6" },
    });
    const form = el({ tagName: "form", parentElement: root });
    const host = el({
      parentElement: form,
      attributes: { unique_id: "otp-host" },
    });
    const shadow = new global.ShadowRoot();
    shadow.host = host;
    shadow.children = [];
    shadow.getRootNode = () => shadow;
    const ownerDocument = { documentElement: root, querySelector: () => null };
    root.children = [form];
    form.children = [host];
    host.shadowRoot = shadow;
    context.document = ownerDocument;
    context.window.globalListnerFlag = true;
    context.window.globalDomDepthMap = new Map();
    context.window.globalHoverStylesMap = new Map();
    context.window.globalParsedElementCounter = {
      get: async () => 100,
      add: async () => {},
    };
    const reflected = [
      "value",
      "aria-valuenow",
      "aria-valuetext",
      "data-value",
      "defaultvalue",
      "placeholder",
    ];
    const inputs = Array.from({ length: 6 }, (_, index) => {
      const value = String(6 - index);
      const input = el({
        tagName: "input",
        attributes: {
          unique_id: `progressive-${index}`,
          type: "text",
          maxlength: "1",
          "aria-label": `Digit ${index + 1}`,
          ...Object.fromEntries(reflected.map((name) => [name, value])),
        },
      });
      input.value = value;
      input.type = "text";
      return input;
    });
    const replacement = el({
      tagName: "input",
      attributes: {
        ...inputs[0]._attributes,
        unique_id: "replacement-0",
      },
    });
    replacement.value = "6";
    replacement.type = "text";
    const ordinary = el({
      tagName: "input",
      attributes: {
        unique_id: "ordinary-input",
        type: "text",
        maxlength: "32",
        value: "1234",
        placeholder: "Amount",
      },
    });
    ordinary.value = "1234";
    ordinary.type = "text";
    const wrapper = el({ attributes: { unique_id: "cached-wrapper" } });
    wrapper.children = [inputs[0]];
    for (const node of [...inputs, wrapper, replacement, ordinary]) {
      node.ownerDocument = ownerDocument;
      node.getRootNode = () => shadow;
      node.setAttribute = (name, value) => {
        node._attributes[name] = value;
      };
      Object.defineProperty(node, "attributes", {
        get: () =>
          Object.entries(node._attributes).map(([name, value]) => ({
            name,
            value,
          })),
      });
    }
    inputs[0].parentElement = nested ? wrapper : null;
    const first = nested ? wrapper : inputs[0];
    shadow.children.push(first);
    shadow.firstElementChild = first;
    await context.addIncrementalNodeToMap(shadow, [first]);
    const flatten = (nodes) =>
      nodes.flatMap((node) => [node, ...flatten(node.children || [])]);
    const cachedFirst = flatten(context.window.globalDomDepthMap.get(0)).find(
      (node) => node.id === "progressive-0",
    );
    assert(
      cachedFirst.attributes.value === "6",
      "first snapshot must predate group protection",
    );
    assert(
      !inputs[0].hasAttribute("data-skyvern-otp-box"),
      "first box must mount alone",
    );
    shadow.children.push(...inputs.slice(1));
    await context.addIncrementalNodeToMap(shadow, inputs.slice(1));
    assert(
      inputs.every((input) => input.hasAttribute("data-skyvern-otp-box")),
      "all live boxes must be stamped before collection",
    );
    if (replaced) {
      if (nested) wrapper.children[0] = replacement;
      else shadow.children[0] = replacement;
      inputs[0].parentElement = null;
      replacement.parentElement = nested ? wrapper : null;
      shadow.firstElementChild = shadow.children[0];
      await context.addIncrementalNodeToMap(nested ? wrapper : shadow, [
        replacement,
      ]);
      assert(
        cachedFirst.attributes.value === "6",
        "detached snapshot must still be raw before collection",
      );
    }
    shadow.children.push(ordinary);
    await context.addIncrementalNodeToMap(shadow, [ordinary]);
    const [elements, tree] = await context.getIncrementElements(false);
    delete root._attributes["data-skyvern-otp-filled"];
    let unarmedReads = 0;
    Object.defineProperty(root, "children", {
      get() {
        unarmedReads++;
        return [form];
      },
    });
    await context.getIncrementElements(false);
    test(`OTP incremental ${replaced ? "second" : "progressive"} shadow remount: cached ${nested ? "descendant" : "root"}`, () => {
      for (const representation of [elements, flatten(tree)]) {
        const boxes = representation.filter(
          (node) => node.tagName === "input" && node.id !== "ordinary-input",
        );
        assert(
          boxes.length === (replaced ? 7 : 6),
          "incremental representation must retain every cached box",
        );
        if (replaced)
          assert(
            boxes.some((box) => box.id === "progressive-0"),
            "detached snapshot was pruned",
          );
        const ordinarySnapshot = representation.find(
          (node) => node.id === "ordinary-input",
        );
        assert(
          ordinarySnapshot.attributes.value === "1234" &&
            ordinarySnapshot.attributes.placeholder === "Amount",
          "live ordinary input was masked",
        );
        for (const box of boxes) {
          for (const name of reflected)
            assert(box.attributes[name] === "*", `cached ${name} is unmasked`);
          assert(
            box.attributes["aria-label"].startsWith("Digit "),
            "descriptive label changed",
          );
        }
      }
      assert(
        inputs.every((input, index) => input.value === String(6 - index)),
        "collection changed live values",
      );
      assert(
        replacement.value === "6" && ordinary.value === "1234",
        "collection changed replacement or ordinary input",
      );
      assert(
        unarmedReads === 0,
        `unarmed document traversed ${unarmedReads} times`,
      );
    });
  }
  console.log(`\n${passed + failed} tests: ${passed} passed, ${failed} failed`);
  process.exit(failed > 0 ? 1 : 0);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
