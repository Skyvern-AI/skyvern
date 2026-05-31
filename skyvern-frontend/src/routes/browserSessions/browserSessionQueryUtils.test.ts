import { describe, expect, it } from "vitest";

import { getBrowserSessionRefetchIntervalMs } from "./browserSessionQueryUtils";

describe("getBrowserSessionRefetchIntervalMs", () => {
  it("polls while session status is bootstrapping", () => {
    expect(getBrowserSessionRefetchIntervalMs(undefined)).toBe(2000);
    expect(getBrowserSessionRefetchIntervalMs("created")).toBe(2000);
    expect(getBrowserSessionRefetchIntervalMs("retry")).toBe(2000);
  });

  it("polls slower while session is running", () => {
    expect(getBrowserSessionRefetchIntervalMs("running")).toBe(5000);
  });

  it("stops polling for terminal statuses", () => {
    expect(getBrowserSessionRefetchIntervalMs("completed")).toBe(false);
    expect(getBrowserSessionRefetchIntervalMs("failed")).toBe(false);
    expect(getBrowserSessionRefetchIntervalMs("timeout")).toBe(false);
  });
});
