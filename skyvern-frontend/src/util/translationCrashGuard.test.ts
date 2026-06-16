import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { installTranslationCrashGuard } from "./translationCrashGuard";

describe("installTranslationCrashGuard", () => {
  beforeEach(() => {
    installTranslationCrashGuard();
  });

  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("removes a genuine child normally", () => {
    const parent = document.createElement("div");
    const child = document.createElement("span");
    parent.appendChild(child);

    expect(parent.removeChild(child)).toBe(child);
    expect(parent.childNodes.length).toBe(0);
  });

  it("no-ops removeChild when the node belongs to another parent", () => {
    const parent = document.createElement("div");
    const otherParent = document.createElement("div");
    const displaced = document.createElement("span");
    otherParent.appendChild(displaced);

    expect(() => parent.removeChild(displaced)).not.toThrow();
    expect(parent.removeChild(displaced)).toBe(displaced);
    expect(displaced.parentNode).toBe(otherParent);
  });

  it("inserts before a genuine reference node normally", () => {
    const parent = document.createElement("div");
    const ref = document.createElement("span");
    const node = document.createElement("b");
    parent.appendChild(ref);

    parent.insertBefore(node, ref);
    expect(parent.firstChild).toBe(node);
    expect(parent.childNodes[1]).toBe(ref);
  });

  it("appends instead of throwing when the reference node has another parent", () => {
    const parent = document.createElement("div");
    parent.appendChild(document.createElement("i"));
    const displacedRef = document.createElement("span");
    document.createElement("div").appendChild(displacedRef);
    const node = document.createElement("b");

    expect(() => parent.insertBefore(node, displacedRef)).not.toThrow();
    expect(node.parentNode).toBe(parent);
    expect(parent.lastChild).toBe(node);
  });

  it("treats a null reference node as an append", () => {
    const parent = document.createElement("div");
    const node = document.createElement("b");

    expect(parent.insertBefore(node, null)).toBe(node);
    expect(parent.firstChild).toBe(node);
  });

  it("is idempotent and does not re-wrap the patched methods", () => {
    const patchedRemove = Node.prototype.removeChild;
    const patchedInsert = Node.prototype.insertBefore;

    installTranslationCrashGuard();

    expect(Node.prototype.removeChild).toBe(patchedRemove);
    expect(Node.prototype.insertBefore).toBe(patchedInsert);
  });
});
