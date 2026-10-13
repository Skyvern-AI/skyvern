import { describe, expect, it } from "vitest";

import { getUniqueLabelForExistingNode } from "./workflowEditorUtils";

describe("getUniqueLabelForExistingNode", () => {
  it("returns the label unchanged when it does not collide", () => {
    expect(getUniqueLabelForExistingNode("block_1", ["other"])).toBe("block_1");
    expect(getUniqueLabelForExistingNode("block_1", [])).toBe("block_1");
  });

  it("suffixes on a single collision instead of returning the duplicate", () => {
    // Regression: the loop bound was existingLabels.length + 1, so with one
    // existing label the loop never ran and the colliding label was returned.
    expect(getUniqueLabelForExistingNode("block_1", ["block_1"])).toBe(
      "block_1_2",
    );
  });

  it("finds the next free suffix when earlier suffixes are taken", () => {
    expect(getUniqueLabelForExistingNode("foo", ["foo", "foo_2"])).toBe(
      "foo_3",
    );
    expect(getUniqueLabelForExistingNode("x", ["x", "x_2", "x_3"])).toBe("x_4");
  });

  it("always returns a label not already present", () => {
    for (let n = 1; n <= 25; n++) {
      const existing = [
        "dup",
        ...Array.from({ length: n }, (_, i) => `dup_${i + 2}`),
      ];
      const result = getUniqueLabelForExistingNode("dup", existing);
      expect(existing).not.toContain(result);
    }
  });
});
