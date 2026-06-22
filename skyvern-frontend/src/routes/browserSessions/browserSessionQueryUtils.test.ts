import { describe, expect, it } from "vitest";

import { getBrowserSessionRefetchIntervalMs } from "./browserSessionQueryUtils";

describe("getBrowserSessionRefetchIntervalMs", () => {
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
});
