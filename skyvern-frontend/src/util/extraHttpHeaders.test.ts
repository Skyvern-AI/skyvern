import { describe, expect, it } from "vitest";

import { hasExtraHttpHeaders } from "./extraHttpHeaders";

describe("hasExtraHttpHeaders", () => {
  it("returns false for unset or empty header objects", () => {
    expect(hasExtraHttpHeaders(null)).toBe(false);
    expect(hasExtraHttpHeaders(undefined)).toBe(false);
    expect(hasExtraHttpHeaders({})).toBe(false);
  });

  it("returns true when at least one header is present", () => {
    expect(hasExtraHttpHeaders({ "X-Trace-Id": "abc123" })).toBe(true);
  });

  it("does not treat arrays as header objects", () => {
    expect(
      hasExtraHttpHeaders(["X-Trace-Id"] as unknown as Record<string, unknown>),
    ).toBe(false);
  });
});
