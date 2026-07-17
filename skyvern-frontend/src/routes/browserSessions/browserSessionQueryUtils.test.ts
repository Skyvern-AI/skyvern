import { describe, expect, it } from "vitest";

import { getBrowserSessionRefetchIntervalMs } from "./browserSessionQueryUtils";

describe("getBrowserSessionRefetchIntervalMs", () => {
  const now = Date.parse("2026-07-16T12:00:00.000Z");

  it("polls while session status is bootstrapping", () => {
    expect(getBrowserSessionRefetchIntervalMs(undefined)).toBe(1000);
    expect(getBrowserSessionRefetchIntervalMs({ status: "created" })).toBe(
      1000,
    );
    expect(getBrowserSessionRefetchIntervalMs({ status: "retry" })).toBe(1000);
  });

  it("polls quickly while a running session is waiting for an address", () => {
    expect(getBrowserSessionRefetchIntervalMs({ status: "running" })).toBe(
      1000,
    );
  });

  it("polls slower after a running session has an address", () => {
    expect(
      getBrowserSessionRefetchIntervalMs({
        status: "running",
        browser_address: "ws://127.0.0.1:9222/devtools/browser/test",
      }),
    ).toBe(5000);
  });

  it("stops polling for terminal statuses", () => {
    expect(getBrowserSessionRefetchIntervalMs({ status: "completed" })).toBe(
      false,
    );
    expect(getBrowserSessionRefetchIntervalMs({ status: "failed" })).toBe(
      false,
    );
    expect(getBrowserSessionRefetchIntervalMs({ status: "timeout" })).toBe(
      false,
    );
  });

  it("polls briefly after completion while recordings are unavailable", () => {
    expect(
      getBrowserSessionRefetchIntervalMs(
        {
          status: "completed",
          completed_at: "2026-07-16T11:59:00.000Z",
          recordings: null,
        },
        now,
      ),
    ).toBe(10_000);
  });

  it("treats timezone-less completed_at as UTC regardless of runner timezone", () => {
    expect(
      getBrowserSessionRefetchIntervalMs(
        {
          status: "completed",
          completed_at: "2026-07-16T11:59:00.000000",
          recordings: [],
        },
        now,
      ),
    ).toBe(10_000);
  });

  it("stops post-terminal polling when completed_at is in the future", () => {
    expect(
      getBrowserSessionRefetchIntervalMs(
        {
          status: "completed",
          completed_at: "2026-07-16T12:05:00.000Z",
          recordings: [],
        },
        now,
      ),
    ).toBe(false);
  });

  it.each([
    [
      "recording available",
      {
        completed_at: "2026-07-16T11:59:00.000Z",
        recordings: [
          {
            checksum: "recording-checksum",
            filename: "session.webm",
            modified_at: "2026-07-16T12:00:00.000Z",
            url: "https://example.test/session.webm",
          },
        ],
      },
    ],
    [
      "window expired",
      { completed_at: "2026-07-16T11:57:59.999Z", recordings: [] },
    ],
    ["completion timestamp missing", { completed_at: null, recordings: [] }],
  ])("stops post-terminal polling when %s", (_name, state) => {
    expect(
      getBrowserSessionRefetchIntervalMs(
        { status: "completed", ...state },
        now,
      ),
    ).toBe(false);
  });
});
