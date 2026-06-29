import { describe, expect, it } from "vitest";

import { copilotRunId } from "./copilotRunId";

describe("copilotRunId", () => {
  it("returns the studio run id on the embedded (studio) mount", () => {
    expect(copilotRunId({ embedded: true, studioRunId: "wr_studio" })).toBe(
      "wr_studio",
    );
  });

  it("returns undefined off-studio even when a studio run id is present", () => {
    expect(
      copilotRunId({ embedded: false, studioRunId: "wr_studio" }),
    ).toBeUndefined();
  });

  it("returns undefined when embedded but no run is focused", () => {
    expect(
      copilotRunId({ embedded: true, studioRunId: undefined }),
    ).toBeUndefined();
  });
});
