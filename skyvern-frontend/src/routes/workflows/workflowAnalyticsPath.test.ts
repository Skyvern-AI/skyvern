import { describe, expect, it } from "vitest";

import { buildWorkflowAnalyticsPath } from "./workflowAnalyticsPath";

describe("buildWorkflowAnalyticsPath", () => {
  it("links analytics to the workflow comparison selection", () => {
    expect(buildWorkflowAnalyticsPath("wpid_abc123")).toBe(
      "/analytics?compare=wpid_abc123",
    );
  });

  it("URL-encodes workflow ids before adding them to the query string", () => {
    expect(buildWorkflowAnalyticsPath("wpid space")).toBe(
      "/analytics?compare=wpid+space",
    );
  });
});
