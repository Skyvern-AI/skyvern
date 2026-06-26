/**
 * Behavioral test: removeBoundingBoxes() must survive a Prototype.js-style
 * monkey-patch of Element.prototype.remove.
 *
 * Some pages ship a `remove(element)` polyfill (Prototype.js shape) that
 * ignores `this` and reads arguments[0]. Calling it as `el.remove()` with no
 * arg then throws `Cannot read properties of undefined (reading 'parentNode'
 * / 'removeChild')`. removeBoundingBoxes() must use parentNode.removeChild
 * directly so it never invokes the polyfill.
 *
 * Exit 0 = pass, exit 1 = failures on stderr.
 */

const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(
  path.join(__dirname, "../../skyvern/webeye/scraper/domUtils.js"),
  "utf8",
);
const fnStart = src.indexOf("function removeBoundingBoxes() {");
if (fnStart === -1) throw new Error("removeBoundingBoxes not found");
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
const fnSource = src.substring(fnStart, fnEnd);

function makeDocumentWithContainer({ attachToParent = true } = {}) {
  const container = {
    id: "boundingBoxContainer",
    _parent: null,
    // Prototype.js-style polyfill: takes an `element` argument and ignores
    // `this`. Calling `container.remove()` invokes this with no arg, so
    // `element` is undefined and `element.parentNode` throws TypeError.
    remove: function (element) {
      element.parentNode.removeChild(element);
    },
  };
  const parent = {
    _children: [],
    removeChild(child) {
      const idx = this._children.indexOf(child);
      if (idx >= 0) {
        this._children.splice(idx, 1);
        child._parent = null;
        return child;
      }
      throw new Error("not a child");
    },
  };
  if (attachToParent) {
    parent._children.push(container);
    container._parent = parent;
  }
  Object.defineProperty(container, "parentNode", {
    get() {
      return this._parent;
    },
  });
  const document = {
    _container: container,
    querySelector(sel) {
      return sel === "#boundingBoxContainer" ? container : null;
    },
  };
  return { document, parent, container };
}

function runRemoveBoundingBoxes(document) {
  const fn = new Function(
    "document",
    `${fnSource}\nreturn removeBoundingBoxes();`,
  );
  fn(document);
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

test("Prototype.js-style monkey-patched remove() does not break removeBoundingBoxes", () => {
  const { document, parent, container } = makeDocumentWithContainer();
  runRemoveBoundingBoxes(document);
  assert(
    parent._children.indexOf(container) === -1,
    "container should be detached from its parent",
  );
});

test("orphan #boundingBoxContainer (no parent) is a no-op, not a throw", () => {
  const { document } = makeDocumentWithContainer({ attachToParent: false });
  // Should not throw even when parentNode is null.
  runRemoveBoundingBoxes(document);
});

test("no #boundingBoxContainer in document is a no-op", () => {
  const document = {
    querySelector() {
      return null;
    },
  };
  runRemoveBoundingBoxes(document);
});

console.log(`\n${passed + failed} tests: ${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
