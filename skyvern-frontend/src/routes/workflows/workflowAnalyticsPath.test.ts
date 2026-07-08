import { describe, expect, it } from "vitest";

import { buildWorkflowAnalyticsPath } from "./workflowAnalyticsPath";

describe("buildWorkflowAnalyticsPath", () => {
  it("links analytics to the selected-agent state", () => {
    expect(buildWorkflowAnalyticsPath("wpid_abc123")).toBe(
      "/analytics?workflow=wpid_abc123",
    );
  });

  it("URL-encodes workflow ids before adding them to the query string", () => {
    expect(buildWorkflowAnalyticsPath("wpid space")).toBe(
      "/analytics?workflow=wpid+space",
    );
  });
});
