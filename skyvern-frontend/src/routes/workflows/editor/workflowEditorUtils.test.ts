import { describe, expect, it } from "vitest";

import { getUniqueLabelForExistingNode } from "./workflowEditorUtils";

describe("getUniqueLabelForExistingNode", () => {
  it("appends a suffix when a renamed node collides with one existing label", () => {
    expect(getUniqueLabelForExistingNode("block_1", ["block_1"])).toBe(
      "block_1_2",
    );
  });

  it("keeps incrementing suffixes until the renamed node label is unique", () => {
    expect(getUniqueLabelForExistingNode("foo", ["foo", "foo_2"])).toBe(
      "foo_3",
    );
  });
});
