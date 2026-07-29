import { describe, expect, it } from "vitest";

import { type BlockSearchTarget } from "@/routes/workflows/studio/blockSearch";
import { resolveTimelineBlockJumpNodeId } from "./timelineBlockJump";

const targets: Array<BlockSearchTarget> = [
  { nodeId: "node-a", label: "Login", blockType: null },
  { nodeId: "node-b", label: "Extract data", blockType: null },
];

describe("resolveTimelineBlockJumpNodeId", () => {
  it("returns null when the editor pane is closed, even with a matching block", () => {
    expect(
      resolveTimelineBlockJumpNodeId({
        editorOpen: false,
        targets,
        label: "Login",
      }),
    ).toBeNull();
  });

  it("returns null for a block with no label", () => {
    expect(
      resolveTimelineBlockJumpNodeId({
        editorOpen: true,
        targets,
        label: null,
      }),
    ).toBeNull();
  });

  it("returns null when the label matches no editor node (renamed/deleted since the run)", () => {
    expect(
      resolveTimelineBlockJumpNodeId({
        editorOpen: true,
        targets,
        label: "Gone",
      }),
    ).toBeNull();
  });

  it("returns the matching node id when the editor is open and the label matches", () => {
    expect(
      resolveTimelineBlockJumpNodeId({
        editorOpen: true,
        targets,
        label: "Extract data",
      }),
    ).toBe("node-b");
  });

  it("returns null when the label matches more than one node (ambiguous, degrade to no-op)", () => {
    const duplicated: Array<BlockSearchTarget> = [
      { nodeId: "node-a", label: "Login", blockType: null },
      { nodeId: "node-c", label: "Login", blockType: null },
    ];
    expect(
      resolveTimelineBlockJumpNodeId({
        editorOpen: true,
        targets: duplicated,
        label: "Login",
      }),
    ).toBeNull();
  });
});
