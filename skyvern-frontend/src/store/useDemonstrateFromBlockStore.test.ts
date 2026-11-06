import { describe, expect, it } from "vitest";

import { demonstrationPrefixFromGraph } from "./useDemonstrateFromBlockStore";

describe("demonstrate from a block", () => {
  it("runs the top-level blocks on the path to the target, in run order", () => {
    const nodes = [
      { id: "start", type: "start" },
      { id: "b", data: { label: "open_job" } },
      { id: "a", data: { label: "login" } },
      { id: "c", data: { label: "fill_form" } },
      { id: "c1", parentId: "c", data: { label: "inner" } },
    ];
    const edges = [
      { source: "start", target: "a" },
      { source: "a", target: "b" },
      { source: "b", target: "c" },
      { source: "c", target: "c1" },
    ];
    expect(demonstrationPrefixFromGraph(nodes, edges, "c")).toEqual([
      { id: "a", label: "login" },
      { id: "b", label: "open_job" },
    ]);
    expect(demonstrationPrefixFromGraph(nodes, edges, "c1")).toBeNull();
    expect(
      demonstrationPrefixFromGraph(
        nodes,
        [...edges, { source: "a", target: "c" }],
        "c",
      ),
    ).toBeNull();
    expect(
      demonstrationPrefixFromGraph(
        [
          ...nodes.slice(0, 2),
          { id: "a", type: "conditional" },
          ...nodes.slice(3),
        ],
        edges,
        "c",
      ),
    ).toBeNull();
  });
});
