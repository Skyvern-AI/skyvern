import { describe, expect, test } from "vitest";

import { scanJsonKeys } from "./errorCodeMappingLinter";

describe("scanJsonKeys", () => {
  test("returns empty array for empty source", () => {
    expect(scanJsonKeys("")).toEqual([]);
  });

  test("returns empty array for non-JSON garbage", () => {
    expect(scanJsonKeys("not json at all")).toEqual([]);
  });

  test("single clean key is found with correct offsets", () => {
    const source = `{ "FOO": "bar" }`;
    const keys = scanJsonKeys(source);
    expect(keys).toHaveLength(1);
    expect(keys[0]!.raw).toBe("FOO");
    expect(source.slice(keys[0]!.from, keys[0]!.to)).toBe('"FOO"');
  });

  test("multiple keys across lines are all found", () => {
    const source = `{\n  "FOO": "a",\n  "BAR": "b"\n}`;
    const keys = scanJsonKeys(source);
    expect(keys).toHaveLength(2);
    expect(keys.map((k) => k.raw)).toEqual(["FOO", "BAR"]);
  });

  test("whitespace-bearing key is captured verbatim (with the space)", () => {
    const source = `{ " FOO": "bar" }`;
    const keys = scanJsonKeys(source);
    expect(keys).toHaveLength(1);
    expect(keys[0]!.raw).toBe(" FOO");
    // The linter can now flag it via `raw !== raw.trim()`.
    expect(keys[0]!.raw !== keys[0]!.raw.trim()).toBe(true);
  });

  test("string values (not keys) are NOT mis-identified as keys", () => {
    // The value "BAR" is a string literal but is not followed by a colon,
    // so the regex lookahead `(?=\s*:)` must skip it.
    const source = `{ "FOO": "BAR" }`;
    const keys = scanJsonKeys(source);
    expect(keys.map((k) => k.raw)).toEqual(["FOO"]);
  });

  test("escaped quotes inside keys are handled", () => {
    // Key literal is "\"weird\"": "val"
    const source = `{ "\\"weird\\"": "val" }`;
    const keys = scanJsonKeys(source);
    expect(keys).toHaveLength(1);
    expect(keys[0]!.raw).toBe('"weird"');
  });

  test("key offsets point at the opening quote", () => {
    const source = `{ " FOO": "bar" }`;
    const keys = scanJsonKeys(source);
    expect(source[keys[0]!.from]).toBe('"');
    expect(source[keys[0]!.to - 1]).toBe('"');
  });

  test("tab-prefixed key is captured with the tab", () => {
    const source = `{ "\\tFOO": "bar" }`;
    const keys = scanJsonKeys(source);
    expect(keys[0]!.raw).toBe("\tFOO");
    expect(keys[0]!.raw !== keys[0]!.raw.trim()).toBe(true);
  });

  test("nested object keys are NOT reported (scope matches save-time)", () => {
    // `validateErrorCodeMapping` only iterates top-level keys, so the
    // linter must match that scope — otherwise a nested whitespace key
    // would get an inline squiggle but save cleanly.
    const source = `{\n  "ERR": {\n    " BAD": "x",\n    "GOOD": "y"\n  },\n  " TOP_BAD": "z"\n}`;
    const keys = scanJsonKeys(source);
    expect(keys.map((k) => k.raw)).toEqual(["ERR", " TOP_BAD"]);
  });

  test("keys inside array values are NOT reported", () => {
    const source = `{ "items": [{ "inner": "v" }] }`;
    const keys = scanJsonKeys(source);
    expect(keys.map((k) => k.raw)).toEqual(["items"]);
  });

  test("braces inside string values do not confuse depth tracking", () => {
    // The `}` inside the value would break a naive depth counter.
    const source = `{ "FOO": "some { weird } value", "BAR": "b" }`;
    const keys = scanJsonKeys(source);
    expect(keys.map((k) => k.raw)).toEqual(["FOO", "BAR"]);
  });

  test("escaped quotes inside string values do not confuse depth tracking", () => {
    const source = `{ "FOO": "has \\"quoted\\" text", "BAR": "b" }`;
    const keys = scanJsonKeys(source);
    expect(keys.map((k) => k.raw)).toEqual(["FOO", "BAR"]);
  });

  test("unterminated string stops the scan gracefully", () => {
    const source = `{ "FOO": "unterminated`;
    // Should not throw, should return whatever it found before the
    // unterminated string.
    expect(() => scanJsonKeys(source)).not.toThrow();
  });
});
