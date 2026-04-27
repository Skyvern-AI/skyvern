import { describe, expect, it } from "vitest";
import { formatDefaultValue } from "./formatDefaultValue";

// Regression tests for SKY-8785: JSON default values were being rendered as
// "[object Object]" because of implicit String() coercion on objects/arrays.
describe("formatDefaultValue", () => {
  it("serializes object defaults as JSON instead of [object Object]", () => {
    const result = formatDefaultValue({ foo: "bar" });
    expect(result).not.toContain("[object Object]");
    expect(result).toBe('{"foo":"bar"}');
  });

  it("serializes array defaults as JSON", () => {
    expect(formatDefaultValue([1, 2, 3])).toBe("[1,2,3]");
  });

  it("returns strings unchanged (no double-quoting)", () => {
    expect(formatDefaultValue("hello")).toBe("hello");
  });

  it("formats numbers and booleans as plain strings", () => {
    expect(formatDefaultValue(42)).toBe("42");
    expect(formatDefaultValue(true)).toBe("true");
  });

  it("handles nested objects", () => {
    expect(formatDefaultValue({ a: { b: [1] } })).toBe('{"a":{"b":[1]}}');
  });
});
