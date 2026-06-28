import { describe, expect, test, vi } from "vitest";

import {
  DEBUG_SESSION_EXPIRY_WARNING_THRESHOLD_MS,
  DEBUG_SESSION_RENEWAL_THRESHOLD_MS,
  formatBrowserSessionRemainingTime,
  getBrowserSessionExpiresAtMs,
  getBrowserSessionRemainingMs,
} from "./debugSessionLease";

describe("debugSessionLease", () => {
  test("calculates browser session expiry from started_at and timeout minutes", () => {
    expect(
      getBrowserSessionExpiresAtMs({
        started_at: "2026-06-23T10:00:00.000Z",
        timeout: 20,
      }),
    ).toBe(new Date("2026-06-23T10:20:00.000Z").getTime());
  });

  test("returns null when the session has not started or has no timeout", () => {
    expect(
      getBrowserSessionExpiresAtMs({ started_at: null, timeout: 20 }),
    ).toBe(null);
    expect(
      getBrowserSessionExpiresAtMs({
        started_at: "2026-06-23T10:00:00.000Z",
        timeout: null,
      }),
    ).toBe(null);
  });

  test("calculates remaining time", () => {
    expect(
      getBrowserSessionRemainingMs(
        {
          started_at: "2026-06-23T10:00:00.000Z",
          timeout: 20,
        },
        new Date("2026-06-23T10:17:30.000Z").getTime(),
      ),
    ).toBe(150_000);
  });

  test("uses Date.now by default when calculating remaining time", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-06-23T10:17:30.000Z"));
      expect(
        getBrowserSessionRemainingMs({
          started_at: "2026-06-23T10:00:00.000Z",
          timeout: 20,
        }),
      ).toBe(150_000);
    } finally {
      vi.useRealTimers();
    }
  });

  test("formats remaining time for warning copy", () => {
    expect(formatBrowserSessionRemainingTime(59_000)).toBe(
      "less than 1 minute",
    );
    expect(formatBrowserSessionRemainingTime(60_000)).toBe("1 minute");
    expect(formatBrowserSessionRemainingTime(61_000)).toBe("1 minute");
    expect(formatBrowserSessionRemainingTime(120_000)).toBe("2 minutes");
  });

  test("warns before the backend renewal cutoff", () => {
    // These values intentionally mirror the backend debug-session renewal cutoff.
    expect(DEBUG_SESSION_RENEWAL_THRESHOLD_MS).toBe(10 * 60 * 1000);
    expect(DEBUG_SESSION_EXPIRY_WARNING_THRESHOLD_MS).toBe(12 * 60 * 1000);
    expect(DEBUG_SESSION_EXPIRY_WARNING_THRESHOLD_MS).toBeGreaterThan(
      DEBUG_SESSION_RENEWAL_THRESHOLD_MS,
    );
  });
});
