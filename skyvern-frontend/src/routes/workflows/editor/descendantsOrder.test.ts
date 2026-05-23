import fc from "fast-check";
import { describe, expect, test } from "vitest";

import type { AppNode } from "./nodes";
import { descendants } from "./workflowEditorUtils";

// Generate a random tree of (id, parentId) nodes rooted at "root".
const arbTree = fc
  .integer({ min: 1, max: 30 })
  .chain((size) =>
    fc.tuple(
      fc.constant(size),
      fc.array(fc.integer({ min: 0 }), { minLength: size, maxLength: size }),
    ),
  )
  .map(([size, parentIndices]) => {
    const nodes: Array<AppNode> = [
      {
        id: "root",
        type: "loop",
        position: { x: 0, y: 0 },
        data: { label: "root" },
      } as AppNode,
    ];

    for (let i = 1; i <= size; i += 1) {
      const parentIdx = parentIndices[i - 1]! % i;
      const parentId = parentIdx === 0 ? "root" : `n${parentIdx}`;
      nodes.push({
        id: `n${i}`,
        type: "task",
        position: { x: 0, y: i * 10 },
        parentId,
        data: { label: `n${i}` },
      } as AppNode);
    }

    return nodes;
  });

describe("descendants() ordering invariant", () => {
  test("returns nodes parent-first: every descendant appears after its parent", () => {
    fc.assert(
      fc.property(arbTree, (nodes) => {
        const result = descendants(nodes, "root");
        const indexById = new Map<string, number>();
        result.forEach((node, index) => indexById.set(node.id, index));

        result.forEach((node) => {
          if (!node.parentId || node.parentId === "root") return;

          const parentIdx = indexById.get(node.parentId);
          if (parentIdx === undefined) return;

          expect(parentIdx).toBeLessThan(indexById.get(node.id)!);
        });
      }),
      { numRuns: 200 },
    );
  });
});
