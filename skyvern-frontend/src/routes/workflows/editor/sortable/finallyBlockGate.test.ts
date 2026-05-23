import { describe, expect, test } from "vitest";

import {
  findFinallyBlockNodeId,
  isBlockFinallyGated,
  type FinallyBlockCandidateNode,
} from "./finallyBlockGate";

describe("isBlockFinallyGated (grip-hide predicate, SKY-9060)", () => {
  test("returns true when the block label matches the configured finally label", () => {
    // NodeHeader uses this to suppress the grip handle, so the finally
    // block surfaces no drag affordance even when the SortableContext has
    // registered its id.
    expect(isBlockFinallyGated("cleanup", "cleanup")).toBe(true);
  });

  test("returns false for a non-finally block when the workflow has a finally label", () => {
    expect(isBlockFinallyGated("scrape", "cleanup")).toBe(false);
  });

  test("returns false when the workflow has no finally block configured", () => {
    // The default workflow state — no finally block — must keep every
    // block draggable, so the predicate collapses to false.
    expect(isBlockFinallyGated("cleanup", null)).toBe(false);
  });

  test("returns false when finallyBlockLabel is an empty string", () => {
    // Empty-string is treated as unset (WorkflowSettingsStore only stores
    // a trimmed, non-empty label) so drag is not gated.
    expect(isBlockFinallyGated("cleanup", "")).toBe(false);
  });

  test("label comparison is case sensitive", () => {
    // Block labels in Skyvern are case-sensitive identifiers. A near-match
    // must not trigger the gate; otherwise a user could accidentally gate
    // the wrong block.
    expect(isBlockFinallyGated("Cleanup", "cleanup")).toBe(false);
  });
});

describe("findFinallyBlockNodeId (drop-target resolver, SKY-9060)", () => {
  function makeNode(
    id: string,
    opts: {
      label?: string;
      parentId?: string;
      type?: string;
    } = {},
  ): FinallyBlockCandidateNode {
    return {
      id,
      type: opts.type ?? "task",
      ...(opts.parentId !== undefined ? { parentId: opts.parentId } : {}),
      ...(opts.label !== undefined ? { data: { label: opts.label } } : {}),
    };
  }

  test("resolves the top-level block whose label matches the finally setting", () => {
    const nodes: Array<FinallyBlockCandidateNode> = [
      makeNode("start-id", { type: "start" }),
      makeNode("a-id", { label: "scrape" }),
      makeNode("b-id", { label: "cleanup" }),
      makeNode("adder-id", { type: "nodeAdder" }),
    ];
    expect(findFinallyBlockNodeId(nodes, "cleanup")).toBe("b-id");
  });

  test("returns null when finallyBlockLabel is null", () => {
    const nodes: Array<FinallyBlockCandidateNode> = [
      makeNode("a-id", { label: "scrape" }),
    ];
    expect(findFinallyBlockNodeId(nodes, null)).toBeNull();
  });

  test("returns null when no top-level block matches the label", () => {
    const nodes: Array<FinallyBlockCandidateNode> = [
      makeNode("a-id", { label: "scrape" }),
      makeNode("b-id", { label: "extract" }),
    ];
    expect(findFinallyBlockNodeId(nodes, "cleanup")).toBeNull();
  });

  test("ignores nested blocks even if the label matches", () => {
    // `finallyBlockLabel` is a workflow-root concept — only top-level
    // blocks should be gated. A matching label on a loop / conditional
    // child must not be picked up, otherwise the top-level rewire would
    // refuse drops against an id that isn't even in the top-level scope.
    const nodes: Array<FinallyBlockCandidateNode> = [
      makeNode("loop-1", { type: "loop" }),
      makeNode("inner-id", { label: "cleanup", parentId: "loop-1" }),
    ];
    expect(findFinallyBlockNodeId(nodes, "cleanup")).toBeNull();
  });

  test("ignores start and nodeAdder anchor nodes with a matching label", () => {
    // Guards against a malformed workflow setting that names the anchor
    // nodes' internal labels (__start_block__, etc.) — those must never be
    // gated as the finally block.
    const nodes: Array<FinallyBlockCandidateNode> = [
      makeNode("start-id", { type: "start", label: "__start_block__" }),
      makeNode("adder-id", { type: "nodeAdder", label: "__start_block__" }),
      makeNode("cleanup-id", { label: "__start_block__" }),
    ];
    // Only the user-authored block should match; the anchors are skipped.
    expect(findFinallyBlockNodeId(nodes, "__start_block__")).toBe("cleanup-id");
  });

  test("returns null when the label matches only nested blocks", () => {
    // Edge case of the two rules combined: no top-level block has the
    // label, so the resolver refuses to up-scope to a nested match.
    const nodes: Array<FinallyBlockCandidateNode> = [
      makeNode("loop-1", { type: "loop" }),
      makeNode("inner-id", { label: "cleanup", parentId: "loop-1" }),
      makeNode("other-id", { label: "scrape" }),
    ];
    expect(findFinallyBlockNodeId(nodes, "cleanup")).toBeNull();
  });

  test("returns the first top-level match if multiple top-level labels collide", () => {
    // Label collisions are guarded at validation time, but the resolver
    // must still be deterministic if two top-level nodes share a label so
    // the gate doesn't crash. First-wins matches the order the editor
    // builds its chain.
    const nodes: Array<FinallyBlockCandidateNode> = [
      makeNode("first-id", { label: "cleanup" }),
      makeNode("second-id", { label: "cleanup" }),
    ];
    expect(findFinallyBlockNodeId(nodes, "cleanup")).toBe("first-id");
  });
});
