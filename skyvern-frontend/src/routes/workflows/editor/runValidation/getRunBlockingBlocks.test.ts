import { describe, it, expect } from "vitest";

import type { AppNode } from "../nodes";
import { getRunBlockingBlocks } from "./getRunBlockingBlocks";

function loginNode(
  id: string,
  label: string,
  parameterKeys: Array<string>,
  extra: Record<string, unknown> = {},
): AppNode {
  const { parentId, ...data } = extra as { parentId?: string };
  return {
    id,
    type: "login",
    parentId,
    position: { x: 0, y: 0 },
    data: { label, parameterKeys, ...data },
  } as unknown as AppNode;
}

function taskNode(id: string, label: string): AppNode {
  return {
    id,
    type: "task",
    position: { x: 0, y: 0 },
    data: { label },
  } as unknown as AppNode;
}

function loopNode(id: string, label: string, parentId?: string): AppNode {
  return {
    id,
    type: "loop",
    parentId,
    position: { x: 0, y: 0 },
    data: { label },
  } as unknown as AppNode;
}

function conditionalNode(
  id: string,
  label: string,
  branches: Array<{ id: string; is_default: boolean; description?: string }>,
  parentId?: string,
): AppNode {
  return {
    id,
    type: "conditional",
    parentId,
    position: { x: 0, y: 0 },
    data: { label, branches },
  } as unknown as AppNode;
}

describe("getRunBlockingBlocks", () => {
  const run = (nodes: Array<AppNode>) => getRunBlockingBlocks(nodes);

  it("returns id + label + empty location for a top-level login with no credential", () => {
    expect(run([loginNode("n1", "block_2", [])])).toEqual([
      { id: "n1", label: "block_2", ancestorIds: [], path: [] },
    ]);
  });

  it("does not flag login blocks that have a credential", () => {
    expect(run([loginNode("n1", "block_2", ["cred_param"])])).toEqual([]);
  });

  it("returns every offending login block, preserving node identity", () => {
    const nodes = [
      loginNode("n1", "block_2", []),
      taskNode("n2", "block_1"),
      loginNode("n3", "block_3", []),
      loginNode("n4", "block_4", ["cred"]),
    ];
    expect(run(nodes).map((block) => block.id)).toEqual(["n1", "n3"]);
  });

  it("does not block a login that binds a plain workflow parameter", () => {
    expect(run([loginNode("n1", "block_2", ["username"])])).toEqual([]);
  });

  it("does not count a URL parameter as a non-URL binding", () => {
    expect(
      run([loginNode("n1", "block_2", ["login_url"], { url: "login_url" })]),
    ).toEqual([{ id: "n1", label: "block_2", ancestorIds: [], path: [] }]);
  });

  it("never flags non-login blocks", () => {
    expect(run([taskNode("n1", "block_1")])).toEqual([]);
  });

  it("counts a login nested in a loop and records its loop ancestor", () => {
    const nodes = [
      loopNode("loop1", "loop_1"),
      loginNode("n1", "block_2", [], { parentId: "loop1" }),
    ];
    expect(run(nodes)).toEqual([
      {
        id: "n1",
        label: "block_2",
        ancestorIds: ["loop1"],
        path: [{ kind: "loop", label: "loop_1" }],
      },
    ]);
  });

  it("records the branch a conditionally-nested login lives on", () => {
    const nodes = [
      conditionalNode("c1", "cond_1", [
        { id: "b1", is_default: false, description: "is ready" },
        { id: "b2", is_default: true },
      ]),
      loginNode("n1", "block_3", [], {
        parentId: "c1",
        conditionalNodeId: "c1",
        conditionalBranchId: "b1",
      }),
    ];
    expect(run(nodes)).toEqual([
      {
        id: "n1",
        label: "block_3",
        ancestorIds: ["c1"],
        path: [{ kind: "conditional", label: "cond_1", branch: "is ready" }],
      },
    ]);
  });

  it("builds a root→leaf path for loop → conditional → login", () => {
    const nodes = [
      loopNode("loop1", "loop_1"),
      conditionalNode(
        "c1",
        "cond_1",
        [
          { id: "b1", is_default: false },
          { id: "b2", is_default: true },
        ],
        "loop1",
      ),
      loginNode("n1", "block_3", [], {
        parentId: "c1",
        conditionalNodeId: "c1",
        conditionalBranchId: "b2",
      }),
    ];
    const [block] = run(nodes);
    expect(block?.ancestorIds).toEqual(["c1", "loop1"]);
    expect(block?.path).toEqual([
      { kind: "loop", label: "loop_1" },
      { kind: "conditional", label: "cond_1", branch: "else" },
    ]);
  });
});
