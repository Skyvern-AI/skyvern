import { describe, test, expect } from "vitest";
import { stableStringify } from "./stableStringify";

describe("stableStringify", () => {
  test("produces identical output regardless of top-level key order", () => {
    expect(stableStringify({ a: 1, b: 2 })).toBe(
      stableStringify({ b: 2, a: 1 }),
    );
  });

  test("produces identical output regardless of nested key order", () => {
    const a = { outer: { x: 1, y: 2 } };
    const b = { outer: { y: 2, x: 1 } };
    expect(stableStringify(a)).toBe(stableStringify(b));
  });

  test("preserves nested keys (does not strip them)", () => {
    const result = stableStringify({
      block_type: "task",
      parameters: [{ key: "name", value: "Alice" }],
    });
    expect(result).toContain('"key":"name"');
    expect(result).toContain('"value":"Alice"');
  });

  test("detects differences inside nested objects", () => {
    const a = {
      block_type: "task",
      label: "Block 6",
      navigation_goal: "Click submit",
      parameters: [{ key: "name", value: "Alice" }],
    };
    const b = {
      block_type: "task",
      label: "Block 6",
      navigation_goal: "Click submit",
      parameters: [{ key: "name", value: "Bob" }],
    };
    expect(stableStringify(a)).not.toBe(stableStringify(b));
  });

  test("detects differences inside record-typed fields", () => {
    const a = { error_code_mapping: { e1: "msg-old" } };
    const b = { error_code_mapping: { e1: "msg-new" } };
    expect(stableStringify(a)).not.toBe(stableStringify(b));
  });

  test("preserves array order (does not sort arrays)", () => {
    expect(stableStringify([1, 2, 3])).toBe("[1,2,3]");
    expect(stableStringify([1, 2, 3])).not.toBe(stableStringify([3, 2, 1]));
  });

  test("handles primitives and null", () => {
    expect(stableStringify(null)).toBe("null");
    expect(stableStringify(42)).toBe("42");
    expect(stableStringify("hi")).toBe('"hi"');
    expect(stableStringify(true)).toBe("true");
  });

  test("omits undefined object values like JSON.stringify", () => {
    expect(stableStringify({ a: undefined, b: 1 })).toBe('{"b":1}');
  });

  test("omit predicate strips matching keys at every depth", () => {
    const result = stableStringify(
      { keep: 1, drop: 2, nested: { keep: 3, drop: 4 } },
      { omit: (k) => k === "drop" },
    );
    expect(result).toBe('{"keep":1,"nested":{"keep":3}}');
  });

  test("omit predicate does not omit the root", () => {
    const result = stableStringify({ keep: 1 }, { omit: () => true });
    expect(result).toBe("{}");
  });

  test("returns undefined for non-serializable top-level values", () => {
    expect(stableStringify(undefined)).toBeUndefined();
    expect(stableStringify(() => 1)).toBeUndefined();
    expect(stableStringify(Symbol("x"))).toBeUndefined();
  });
});
