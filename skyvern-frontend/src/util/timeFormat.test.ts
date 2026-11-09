// ABOUTME: Tests for timeFormat utility functions used across the frontend.
// ABOUTME: Ensures UTC timestamps (with and without 'Z' suffix) are parsed correctly.
import { describe, test, expect } from "vitest";
import {
  basicLocalTimeFormat,
  basicTimeFormat,
  formatDurationSeconds,
  formatExecutionTime,
  formatSeconds,
  normalizeUtcTimestamp,
} from "./timeFormat";
import { formatElapsedSeconds } from "./utils";

describe("normalizeUtcTimestamp", () => {
  test("appends Z to timezone-less timestamps", () => {
    expect(normalizeUtcTimestamp("2026-03-04T18:30:00.123456")).toBe(
      "2026-03-04T18:30:00.123Z",
    );
  });

  test("leaves offset-carrying timestamps intact", () => {
    expect(normalizeUtcTimestamp("2026-03-04T18:30:00+00:00")).toBe(
      "2026-03-04T18:30:00+00:00",
    );
    expect(normalizeUtcTimestamp("2026-03-04T18:30:00-07:00")).toBe(
      "2026-03-04T18:30:00-07:00",
    );
    expect(normalizeUtcTimestamp("2026-03-04T18:30:00Z")).toBe(
      "2026-03-04T18:30:00Z",
    );
  });
});

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

describe("formatSeconds", () => {
  test.each([
    [0, "0s"],
    [45.4, "45s"],
    [59.6, "1m 0s"],
    [165, "2m 45s"],
    [3600, "1h 0m"],
    [3930, "1h 5m"],
    [26 * 3600, "1d 2h"],
    [40 * 86400 + 3 * 3600 + 59, "40d 3h"],
    [-5, "0s"],
  ])("%s -> %s", (seconds, expected) => {
    expect(formatSeconds(seconds)).toBe(expected);
  });
});

describe("formatElapsedSeconds", () => {
  test.each([
    [5_000, "0:05"],
    [125_000, "2:05"],
    [3_725_000, "1:02:05"],
    [26 * 3_600_000, "26:00:00"],
  ])("%s ms -> %s", (ms, expected) => {
    expect(formatElapsedSeconds(ms)).toBe(expected);
  });
});

describe("formatDurationSeconds", () => {
  test("formats valid durations and leaves unavailable values blank", () => {
    expect(formatDurationSeconds(42.4)).toBe("42s");
    expect(formatDurationSeconds(125.2)).toBe("2m 5s");
    expect(formatDurationSeconds(119.6)).toBe("2m 0s");
    expect(formatDurationSeconds(59.6)).toBe("1m 0s");
    expect(formatDurationSeconds(null)).toBe("—");
    expect(formatDurationSeconds(-1)).toBe("—");
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

  test("renders the timestamp in UTC, not the viewer's local time", () => {
    // The tooltip is labeled "UTC", so it must show the UTC time regardless of
    // the machine's time zone. For an input carrying a Z the rendered time must
    // equal the UTC time (18:30), never a local-zone conversion of it.
    const result = basicTimeFormat("2026-03-04T18:30:00Z");
    expect(result).toBe("Wed, Mar 4, 2026 at 6:30:00 PM UTC");
  });

  test("treats a no-suffix timestamp as UTC before rendering", () => {
    // Backend returns naive UTC timestamps without 'Z'. normalizeUtcTimestamp
    // must run first so the tooltip matches the Z-suffixed instant.
    const withoutZ = "2026-03-04T18:30:00.000000";
    const withZ = "2026-03-04T18:30:00.000000Z";
    expect(basicTimeFormat(withoutZ)).toBe(basicTimeFormat(withZ));
  });
});
