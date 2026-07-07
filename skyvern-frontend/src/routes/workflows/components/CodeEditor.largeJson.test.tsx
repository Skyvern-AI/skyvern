import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { CodeEditor } from "./CodeEditor";
import {
  isDeeplyNestedDocument,
  isOversizedDocument,
} from "./oversizedDocument";

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

// Big enough to cross LARGE_DOCUMENT_CHAR_THRESHOLD (50k) but structurally
// shallow (depth ~3), like a data-heavy webhook payload. These are safe to
// highlight — CodeMirror renders/highlights only the visible viewport, and the
// SKY-11432 overflow is driven by decoration nesting depth, not raw size.
function largeShallowJson(): string {
  const items = Array.from({ length: 4000 }, (_, i) => ({
    id: i,
    name: `item-${i}`,
  }));
  return JSON.stringify({ items }, null, 2);
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

  it("treats an absent (undefined/null) document as not oversized", () => {
    expect(isOversizedDocument(undefined)).toBe(false);
    expect(isOversizedDocument(null)).toBe(false);
  });
});

describe("isDeeplyNestedDocument", () => {
  it("treats small, shallow JSON as not deeply nested", () => {
    const value = JSON.stringify({ a: 1, b: [1, 2, 3], c: "hi" }, null, 2);
    expect(isDeeplyNestedDocument(value)).toBe(false);
  });

  it("does not flag large but shallow JSON (keeps highlighting)", () => {
    const value = largeShallowJson();
    expect(value.length).toBeGreaterThan(50_000);
    expect(isDeeplyNestedDocument(value)).toBe(false);
  });

  it("flags deeply nested JSON beyond the depth cap", () => {
    expect(isDeeplyNestedDocument(deeplyNestedJson(250))).toBe(true);
  });

  it("does not flag moderately nested JSON within the depth cap", () => {
    expect(isDeeplyNestedDocument(deeplyNestedJson(20))).toBe(false);
  });

  it("treats an absent (undefined/null) document as not deeply nested", () => {
    expect(isDeeplyNestedDocument(undefined)).toBe(false);
    expect(isDeeplyNestedDocument(null)).toBe(false);
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

  it("keeps highlighting but disables wrapping for large, shallow JSON", () => {
    const value = largeShallowJson();
    const { container } = render(<CodeEditor value={value} language="json" />);
    expect(highlightTokenSpanCount(container)).toBeGreaterThan(0);
    expect(isLineWrapping(container)).toBe(false);
  });

  it("renders deeply nested JSON without throwing", () => {
    const value = deeplyNestedJson(5_000);
    expect(() =>
      render(<CodeEditor value={value} language="json" />),
    ).not.toThrow();
  });

  it("renders without throwing when the document value is absent", () => {
    expect(() =>
      render(
        <CodeEditor value={undefined as unknown as string} language="json" />,
      ),
    ).not.toThrow();
  });
});
