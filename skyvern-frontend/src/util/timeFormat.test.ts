// ABOUTME: Tests for timeFormat utility functions used across the frontend.
// ABOUTME: Ensures UTC timestamps (with and without 'Z' suffix) are parsed correctly.
import { describe, test, expect } from "vitest";
import {
  basicLocalTimeFormat,
  basicTimeFormat,
  formatExecutionTime,
} from "./timeFormat";

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

describe("formatExecutionTime", () => {
  const base = "2026-01-01T00:00:00.000Z";

  test("returns null when finishedAt is null", () => {
    expect(formatExecutionTime(base, null)).toBeNull();
  });

  test("returns null when createdAt is an invalid date string", () => {
    expect(
      formatExecutionTime("not-a-date", "2026-01-01T00:00:05.000Z"),
    ).toBeNull();
  });

  test("returns null when finishedAt is an invalid date string", () => {
    expect(formatExecutionTime(base, "not-a-date")).toBeNull();
  });

  test("returns seconds for sub-minute duration", () => {
    const finishedAt = "2026-01-01T00:00:45.000Z";
    expect(formatExecutionTime(base, finishedAt)).toBe("45s");
  });

  test("returns minutes and seconds for durations under an hour", () => {
    const finishedAt = "2026-01-01T00:02:45.000Z";
    expect(formatExecutionTime(base, finishedAt)).toBe("2m 45s");
  });

  test("returns minutes only when seconds is zero", () => {
    const finishedAt = "2026-01-01T00:02:00.000Z";
    expect(formatExecutionTime(base, finishedAt)).toBe("2m");
  });

  test("returns hours and minutes for multi-hour duration", () => {
    const finishedAt = "2026-01-01T01:05:30.000Z";
    expect(formatExecutionTime(base, finishedAt)).toBe("1h 5m");
  });

  test("returns hours only when minutes is zero", () => {
    const finishedAt = "2026-01-01T02:00:00.000Z";
    expect(formatExecutionTime(base, finishedAt)).toBe("2h");
  });

  test("drops seconds for hour-plus durations when minutes are zero", () => {
    const finishedAt = "2026-01-01T01:00:30.000Z";
    expect(formatExecutionTime(base, finishedAt)).toBe("1h");
  });

  test("normalizes UTC timestamps without Z suffix", () => {
    const createdWithoutZ = "2026-01-01T00:00:00.000000";
    const finishedWithoutZ = "2026-01-01T00:02:45.000000";
    expect(formatExecutionTime(createdWithoutZ, finishedWithoutZ)).toBe(
      "2m 45s",
    );
  });

  test("clamps negative duration to 0s when finishedAt is before createdAt", () => {
    const finishedAt = "2025-12-31T23:59:59.000Z";
    expect(formatExecutionTime(base, finishedAt)).toBe("0s");
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
