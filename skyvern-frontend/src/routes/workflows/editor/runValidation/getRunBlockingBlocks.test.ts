import { describe, it, expect } from "vitest";

import type { AppNode } from "../nodes";
import { getRunBlockingBlocks } from "./getRunBlockingBlocks";

function loginNode(
  id: string,
  label: string,
  parameterKeys: Array<string>,
): AppNode {
  return {
    id,
    type: "login",
    position: { x: 0, y: 0 },
    data: { label, parameterKeys },
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

describe("getRunBlockingBlocks", () => {
  it("returns id + label for login blocks with no credential", () => {
    expect(getRunBlockingBlocks([loginNode("n1", "block_2", [])])).toEqual([
      { id: "n1", label: "block_2" },
    ]);
  });

  it("does not flag login blocks that have a credential", () => {
    expect(
      getRunBlockingBlocks([loginNode("n1", "block_2", ["cred_param"])]),
    ).toEqual([]);
  });

  it("returns every offending login block, preserving node identity", () => {
    const nodes = [
      loginNode("n1", "block_2", []),
      taskNode("n2", "block_1"),
      loginNode("n3", "block_3", []),
      loginNode("n4", "block_4", ["cred"]),
    ];
    expect(getRunBlockingBlocks(nodes)).toEqual([
      { id: "n1", label: "block_2" },
      { id: "n3", label: "block_3" },
    ]);
  });

  it("never flags non-login blocks", () => {
    expect(getRunBlockingBlocks([taskNode("n1", "block_1")])).toEqual([]);
  });
});
