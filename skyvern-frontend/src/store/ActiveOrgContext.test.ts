import { describe, expect, it } from "vitest";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
} from "./ActiveOrgContext";

describe("getOrgScopedQueryKey", () => {
  it("preserves existing query keys when the caller explicitly chooses the unscoped fallback", () => {
    const queryKey = ["workflowRun", "wr_123"] as const;
    const activeOrgScope = getActiveOrgQueryKeyScope(undefined);

    expect(getOrgScopedQueryKey(queryKey, activeOrgScope)).toBe(queryKey);
  });

  it("appends the active org id when one is provided", () => {
    const activeOrgScope = getActiveOrgQueryKeyScope("org_a");

    expect(getOrgScopedQueryKey(["runs", 1] as const, activeOrgScope)).toEqual([
      "runs",
      1,
      "org_a",
    ]);
  });
});
