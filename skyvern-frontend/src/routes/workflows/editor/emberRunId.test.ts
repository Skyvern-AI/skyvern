import { describe, expect, it } from "vitest";

import { emberRunId } from "./emberRunId";

describe("emberRunId", () => {
  it("returns the studio run id on the embedded (studio) mount", () => {
    expect(emberRunId({ embedded: true, studioRunId: "wr_studio" })).toBe(
      "wr_studio",
    );
  });

  it("returns undefined off-studio even when a studio run id is present", () => {
    expect(
      emberRunId({ embedded: false, studioRunId: "wr_studio" }),
    ).toBeUndefined();
  });

  it("returns undefined when embedded but no run is focused", () => {
    expect(
      emberRunId({ embedded: true, studioRunId: undefined }),
    ).toBeUndefined();
  });
});
