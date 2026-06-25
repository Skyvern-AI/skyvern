import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { CodeEditor } from "./CodeEditor";
import { isOversizedDocument } from "./oversizedDocument";

// CodeEditor mounts the real editor immediately only when IntersectionObserver
// is absent; otherwise it renders a lazy-mount placeholder with no `.cm-content`.
// Force the global off so these real-DOM assertions are deterministic even if a
// polyfill (or a future jsdom) provides one.
let savedIntersectionObserver: typeof IntersectionObserver | undefined;
beforeEach(() => {
  savedIntersectionObserver = globalThis.IntersectionObserver;
  // @ts-expect-error deleting an optional global for test determinism
  delete globalThis.IntersectionObserver;
});
afterEach(() => {
  if (savedIntersectionObserver) {
    globalThis.IntersectionObserver = savedIntersectionObserver;
  }
});

// style-mod (used by CodeMirror) prefixes every generated highlight-token
// class with U+037C, so syntax highlighting is active iff a span inside a line
// carries such a class. Plain text only yields structural spans like
// `cm-matchingBracket`.
const STYLE_MOD_PREFIX = "ͼ";

function highlightTokenSpanCount(container: HTMLElement): number {
  return Array.from(container.querySelectorAll(".cm-line span")).filter(
    (span) => (span.getAttribute("class") ?? "").includes(STYLE_MOD_PREFIX),
  ).length;
}

function isLineWrapping(container: HTMLElement): boolean {
  return Boolean(
    container
      .querySelector(".cm-content")
      ?.classList.contains("cm-lineWrapping"),
  );
}

function deeplyNestedJson(depth: number): string {
  return "[".repeat(depth) + "1" + "]".repeat(depth);
}

describe("isOversizedDocument", () => {
  it("treats small, shallow JSON as normal", () => {
    const value = JSON.stringify({ a: 1, b: [1, 2, 3], c: "hi" }, null, 2);
    expect(isOversizedDocument(value)).toBe(false);
  });

  it("flags documents longer than the character threshold", () => {
    const value = `"${"a".repeat(60_000)}"`;
    expect(isOversizedDocument(value)).toBe(true);
  });

  it("flags deeply nested JSON beyond the depth cap", () => {
    expect(isOversizedDocument(deeplyNestedJson(250))).toBe(true);
  });

  it("does not flag moderately nested JSON within the depth cap", () => {
    expect(isOversizedDocument(deeplyNestedJson(20))).toBe(false);
  });

  it("ignores brackets inside string literals when measuring depth", () => {
    const value = JSON.stringify({ note: "[".repeat(500) }, null, 2);
    expect(isOversizedDocument(value)).toBe(false);
  });
});

describe("CodeEditor large-document guard", () => {
  it("highlights and wraps normal JSON", () => {
    const value = JSON.stringify({ a: 1, b: [1, 2, 3], c: "hi" }, null, 2);
    const { container } = render(<CodeEditor value={value} language="json" />);
    expect(highlightTokenSpanCount(container)).toBeGreaterThan(0);
    expect(isLineWrapping(container)).toBe(true);
  });

  it("disables highlighting and wrapping for deeply nested JSON", () => {
    const value = deeplyNestedJson(250);
    const { container } = render(<CodeEditor value={value} language="json" />);
    expect(highlightTokenSpanCount(container)).toBe(0);
    expect(isLineWrapping(container)).toBe(false);
  });

  it("disables highlighting and wrapping for very large JSON", () => {
    const value = `"${"a".repeat(60_000)}"`;
    const { container } = render(<CodeEditor value={value} language="json" />);
    expect(highlightTokenSpanCount(container)).toBe(0);
    expect(isLineWrapping(container)).toBe(false);
  });

  it("renders deeply nested JSON without throwing", () => {
    const value = deeplyNestedJson(5_000);
    expect(() =>
      render(<CodeEditor value={value} language="json" />),
    ).not.toThrow();
  });
});
