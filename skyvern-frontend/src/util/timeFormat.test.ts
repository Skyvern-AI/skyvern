// ABOUTME: Tests for timeFormat utility functions used across the frontend.
// ABOUTME: Ensures UTC timestamps (with and without 'Z' suffix) are parsed correctly.
import { describe, test, expect } from "vitest";
import { basicLocalTimeFormat, basicTimeFormat } from "./timeFormat";

describe("basicLocalTimeFormat", () => {
  test("appends Z to parse a no-suffix UTC timestamp as UTC, not local time", () => {
    // Backend returns naive UTC timestamps without 'Z', e.g. "2026-03-04T18:30:00.000000".
    // Without the Z fix, JS would parse this as local time, which is wrong for non-UTC users.
    // We verify the function produces the same output as if the Z were already present.
    const withoutZ = "2026-03-04T18:30:00.000000";
    const withZ = "2026-03-04T18:30:00.000000Z";
    expect(basicLocalTimeFormat(withoutZ)).toBe(basicLocalTimeFormat(withZ));
  });

  test("handles timestamps already ending in Z", () => {
    const result = basicLocalTimeFormat("2026-03-04T18:30:00Z");
    expect(result).toContain("at");
    expect(typeof result).toBe("string");
    expect(result.length).toBeGreaterThan(0);
  });

  test("trims microseconds to milliseconds before parsing", () => {
    const withMicros = "2026-03-04T18:30:00.123456";
    const withMillis = "2026-03-04T18:30:00.123";
    expect(basicLocalTimeFormat(withMicros)).toBe(
      basicLocalTimeFormat(withMillis),
    );
  });
});

describe("basicTimeFormat", () => {
  test("appends UTC label to the formatted string", () => {
    const result = basicTimeFormat("2026-03-04T18:30:00Z");
    expect(result).toMatch(/UTC$/);
  });

  test("includes date and time separated by 'at'", () => {
    const result = basicTimeFormat("2026-03-04T18:30:00Z");
    expect(result).toContain(" at ");
  });
});
