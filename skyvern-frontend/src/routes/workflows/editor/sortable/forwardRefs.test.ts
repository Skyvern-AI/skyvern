import type { Node } from "@xyflow/react";
import { describe, expect, test } from "vitest";

import { findForwardReferenceViolations } from "./forwardRefs";

type TestNode = Node<Record<string, unknown>>;

function block(
  id: string,
  label: string,
  extra: Record<string, unknown> = {},
): TestNode {
  return {
    id,
    type: "task",
    position: { x: 0, y: 0 },
    data: { label, ...extra },
  };
}

function startNode(): TestNode {
  return {
    id: "start",
    type: "start",
    position: { x: 0, y: 0 },
    data: {},
  };
}

function adderNode(): TestNode {
  return {
    id: "adder",
    type: "nodeAdder",
    position: { x: 0, y: 0 },
    data: {},
  };
}

describe("findForwardReferenceViolations", () => {
  test("valid reorder: no references to moved block returns empty", () => {
    const nodes: Array<TestNode> = [
      startNode(),
      block("a", "alpha"),
      block("b", "beta", { navigationGoal: "click a button" }),
      block("c", "gamma", { prompt: "do the thing" }),
      adderNode(),
    ];
    // Move alpha from index 0 to index 2 (original order: a, b, c).
    const newOrder = ["b", "c", "a"];

    const violations = findForwardReferenceViolations({
      nodes,
      newOrder,
      movedNodeId: "a",
    });

    expect(violations).toEqual([]);
  });

  test("valid reorder: referrer still comes after referent returns empty", () => {
    // `c` references `a`. Moving `b` between them keeps `c` after `a`, so the
    // reorder is safe — the scanner must not flag `c`.
    const nodes: Array<TestNode> = [
      startNode(),
      block("a", "alpha"),
      block("b", "beta"),
      block("c", "gamma", { prompt: "use {{alpha}} here" }),
      adderNode(),
    ];
    const newOrder = ["a", "c", "b"];

    const violations = findForwardReferenceViolations({
      nodes,
      newOrder,
      movedNodeId: "b",
    });

    expect(violations).toEqual([]);
  });

  test("reorder-creates-forward-ref: referrer now precedes moved block", () => {
    // `c` references `alpha`. After moving `a` (alpha) to the tail, `c` is at
    // index 1 and `a` is at index 2 — forward ref.
    const nodes: Array<TestNode> = [
      startNode(),
      block("a", "alpha"),
      block("b", "beta"),
      block("c", "gamma", { prompt: "use {{alpha}}" }),
      adderNode(),
    ];
    const newOrder = ["b", "c", "a"];

    const violations = findForwardReferenceViolations({
      nodes,
      newOrder,
      movedNodeId: "a",
    });

    expect(violations).toEqual([
      { referrerNodeId: "c", referrerLabel: "gamma" },
    ]);
  });

  test("reorder-of-referent-itself: multiple referrers now precede it", () => {
    // Both `b` and `c` reference `alpha`. Moving `a` to the tail creates
    // two forward refs — the scanner must surface both so the toast lists
    // every offender.
    const nodes: Array<TestNode> = [
      startNode(),
      block("a", "alpha"),
      block("b", "beta", { navigationGoal: "look up {{alpha}}" }),
      block("c", "gamma", { prompt: "also {{alpha | upper}}" }),
      adderNode(),
    ];
    const newOrder = ["b", "c", "a"];

    const violations = findForwardReferenceViolations({
      nodes,
      newOrder,
      movedNodeId: "a",
    });

    expect(violations).toEqual([
      { referrerNodeId: "b", referrerLabel: "beta" },
      { referrerNodeId: "c", referrerLabel: "gamma" },
    ]);
  });

  test("detects refs nested deep inside objects, not just top-level strings", () => {
    // The Jinja ref is buried inside an array of header objects — the
    // objectContainsJinjaReference walker must still catch it. This is the
    // ticket's 'refs across nested objects' AC.
    const nodes: Array<TestNode> = [
      startNode(),
      block("a", "alpha"),
      block("b", "beta", {
        headers: [
          { name: "X-Run-Id", value: "static" },
          { name: "X-Trace", value: "{{alpha.id}}" },
        ],
      }),
      adderNode(),
    ];
    const newOrder = ["b", "a"];

    const violations = findForwardReferenceViolations({
      nodes,
      newOrder,
      movedNodeId: "a",
    });

    expect(violations).toEqual([
      { referrerNodeId: "b", referrerLabel: "beta" },
    ]);
  });

  test("ignores partial label matches (containsJinjaReference uses negative lookahead)", () => {
    // `alpha_extra` must not collide with `alpha`. The underlying regex uses a
    // negative lookahead on identifier chars, so `{{alpha_extra}}` does NOT
    // reference `alpha` and the scanner must stay silent.
    const nodes: Array<TestNode> = [
      startNode(),
      block("a", "alpha"),
      block("b", "beta", { prompt: "{{alpha_extra}}" }),
      adderNode(),
    ];
    const newOrder = ["b", "a"];

    const violations = findForwardReferenceViolations({
      nodes,
      newOrder,
      movedNodeId: "a",
    });

    expect(violations).toEqual([]);
  });

  test("empty newOrder or unknown movedNodeId yields no violations", () => {
    const nodes: Array<TestNode> = [
      startNode(),
      block("a", "alpha"),
      block("b", "beta", { prompt: "{{alpha}}" }),
      adderNode(),
    ];

    expect(
      findForwardReferenceViolations({
        nodes,
        newOrder: [],
        movedNodeId: "a",
      }),
    ).toEqual([]);

    expect(
      findForwardReferenceViolations({
        nodes,
        newOrder: ["a", "b"],
        movedNodeId: "does-not-exist",
      }),
    ).toEqual([]);
  });

  test("moved-referrer direction: moved block references a block that now comes after it", () => {
    // Covers the symmetric case missed by a referent-only scanner:
    // C references {{alpha}}; dragging C above A reorders to [C, A, B],
    // so C now forward-references A even though no block references C's
    // own label.
    const nodes: Array<TestNode> = [
      startNode(),
      block("a", "alpha"),
      block("b", "beta"),
      block("c", "gamma", { prompt: "use {{alpha}}" }),
      adderNode(),
    ];
    const newOrder = ["c", "a", "b"];
    const result = findForwardReferenceViolations({
      nodes,
      newOrder,
      movedNodeId: "c",
    });
    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({
      referrerNodeId: "c",
      referrerLabel: "gamma",
    });
  });

  test("moved-referrer direction: nested-object ref to later block is caught", () => {
    const nodes: Array<TestNode> = [
      startNode(),
      block("a", "alpha"),
      block("b", "beta"),
      // Ref buried inside a nested structure (mirrors loop prompts / HTTP
      // headers) — must still be caught in the moved-referrer direction.
      block("c", "gamma", {
        params: { headers: { "X-Token": "{{alpha}}" } },
      }),
      adderNode(),
    ];
    const newOrder = ["c", "a", "b"];
    const result = findForwardReferenceViolations({
      nodes,
      newOrder,
      movedNodeId: "c",
    });
    expect(result).toHaveLength(1);
    expect(result[0]?.referrerNodeId).toBe("c");
  });

  test("direction-a: output key reference (A_output) is caught when referrer precedes moved block", () => {
    // B references {{ alpha_output }} (the output key of block A).
    // Dragging A below B makes B execute before A — must be flagged.
    const nodes: Array<TestNode> = [
      startNode(),
      block("a", "alpha"),
      block("b", "beta", { prompt: "use {{alpha_output}}" }),
      adderNode(),
    ];
    const newOrder = ["b", "a"];

    const violations = findForwardReferenceViolations({
      nodes,
      newOrder,
      movedNodeId: "a",
    });

    expect(violations).toEqual([
      { referrerNodeId: "b", referrerLabel: "beta" },
    ]);
  });

  test("direction-b: moved block referencing output key of a later block is caught", () => {
    // C references {{ alpha_output }}; dragging C above A makes C execute
    // before A, so C forward-references A's output.
    const nodes: Array<TestNode> = [
      startNode(),
      block("a", "alpha"),
      block("b", "beta"),
      block("c", "gamma", { prompt: "use {{alpha_output}}" }),
      adderNode(),
    ];
    const newOrder = ["c", "a", "b"];
    const result = findForwardReferenceViolations({
      nodes,
      newOrder,
      movedNodeId: "c",
    });
    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({ referrerNodeId: "c", referrerLabel: "gamma" });
  });

  test("moved-referrer direction: references only to earlier blocks are OK", () => {
    // C references {{alpha}}; A stays before C in the new order, so the
    // scanner must not flag anything.
    const nodes: Array<TestNode> = [
      startNode(),
      block("a", "alpha"),
      block("b", "beta"),
      block("c", "gamma", { prompt: "use {{alpha}}" }),
      adderNode(),
    ];
    const newOrder = ["a", "c", "b"];
    expect(
      findForwardReferenceViolations({
        nodes,
        newOrder,
        movedNodeId: "c",
      }),
    ).toEqual([]);
  });

  test("direction-a: parameterKeys output-key dep is caught when referrer precedes moved block", () => {
    // B picks A_output via ParametersMultiSelect; dragging A below B is a
    // forward ref even though no Jinja template is involved.
    const nodes: Array<TestNode> = [
      startNode(),
      block("a", "alpha"),
      block("b", "beta", { parameterKeys: ["alpha_output"] }),
      adderNode(),
    ];
    const newOrder = ["b", "a"];
    const violations = findForwardReferenceViolations({
      nodes,
      newOrder,
      movedNodeId: "a",
    });
    expect(violations).toEqual([
      { referrerNodeId: "b", referrerLabel: "beta" },
    ]);
  });

  test("direction-b: moved block with parameterKeys output-key dep on later block is caught", () => {
    // C picks A_output via ParametersMultiSelect; dragging C above A is a
    // forward ref in the moved-referrer direction.
    const nodes: Array<TestNode> = [
      startNode(),
      block("a", "alpha"),
      block("b", "beta"),
      block("c", "gamma", { parameterKeys: ["alpha_output"] }),
      adderNode(),
    ];
    const newOrder = ["c", "a", "b"];
    const result = findForwardReferenceViolations({
      nodes,
      newOrder,
      movedNodeId: "c",
    });
    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({ referrerNodeId: "c", referrerLabel: "gamma" });
  });

  test("direction-a: raw-label parameterKeys match is NOT flagged (workflow param collision)", () => {
    // B has parameterKeys: ["alpha"] — this is a workflow parameter named
    // "alpha", not a reference to block A's output, so dragging A below B
    // must not be blocked.
    const nodes: Array<TestNode> = [
      startNode(),
      block("a", "alpha"),
      block("b", "beta", { parameterKeys: ["alpha"] }),
      adderNode(),
    ];
    const newOrder = ["b", "a"];
    expect(
      findForwardReferenceViolations({
        nodes,
        newOrder,
        movedNodeId: "a",
      }),
    ).toEqual([]);
  });
});
