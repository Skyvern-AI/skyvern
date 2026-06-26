import type { NodeDimensionChange } from "@xyflow/react";
import { describe, expect, test } from "vitest";

import type { AppNode } from "./nodes";
import {
  DIMENSION_CONVERGENCE_QUIET_WINDOW_MS,
  MAX_DIMENSION_LAYOUT_PASSES,
  createDimensionConvergenceState,
  processDimensionChanges,
  quantizeDimension,
  resetDimensionConvergence,
} from "./dimensionConvergence";

function makeNode(id: string, width: number, height: number): AppNode {
  return {
    id,
    type: "task",
    position: { x: 0, y: 0 },
    data: { label: id },
    measured: { width, height },
  } as AppNode;
}

function dimensionChange(
  id: string,
  width: number,
  height: number,
): NodeDimensionChange {
  return { id, type: "dimensions", dimensions: { width, height } };
}

describe("quantizeDimension", () => {
  test("rounds to the nearest integer pixel", () => {
    expect(quantizeDimension(200.4)).toBe(200);
    expect(quantizeDimension(200.6)).toBe(201);
  });

  test("passes through undefined", () => {
    expect(quantizeDimension(undefined)).toBeUndefined();
  });
});

describe("processDimensionChanges", () => {
  test("sub-pixel jitter never re-triggers layout", () => {
    const nodes = [makeNode("a", 200, 100)];
    const state = createDimensionConvergenceState();

    let layoutCount = 0;
    for (const subpixel of [200.1, 200.4, 199.7, 200.49, 199.51]) {
      const result = processDimensionChanges(
        nodes,
        [dimensionChange("a", subpixel, 100.2)],
        state,
      );
      if (result.shouldLayout) layoutCount += 1;
    }

    expect(layoutCount).toBe(0);
  });

  test("a single genuine resize triggers exactly one layout", () => {
    const nodes = [makeNode("a", 200, 100)];
    const state = createDimensionConvergenceState();

    const first = processDimensionChanges(
      nodes,
      [dimensionChange("a", 200, 140)],
      state,
    );
    const second = processDimensionChanges(
      nodes,
      [dimensionChange("a", 200, 140)],
      state,
    );

    expect(first.shouldLayout).toBe(true);
    expect(second.shouldLayout).toBe(false);
    expect(nodes[0]!.measured?.height).toBe(140);
  });

  test("a sustained oscillation is bounded by the pass budget", () => {
    const nodes = [makeNode("a", 200, 100)];
    const state = createDimensionConvergenceState();

    let layoutCount = 0;
    for (let i = 0; i < 25; i += 1) {
      const height = i % 2 === 0 ? 120 : 160;
      const result = processDimensionChanges(
        nodes,
        [dimensionChange("a", 200, height)],
        state,
      );
      if (result.shouldLayout) layoutCount += 1;
    }

    expect(layoutCount).toBeLessThanOrEqual(MAX_DIMENSION_LAYOUT_PASSES);
  });

  test("budget re-arms across genuine resizes separated by the quiet window", () => {
    const nodes = [makeNode("a", 200, 100)];
    const state = createDimensionConvergenceState();

    const heights = [140, 180, 220, 260, 300, 340];
    expect(heights.length).toBeGreaterThan(MAX_DIMENSION_LAYOUT_PASSES);

    let layoutCount = 0;
    let now = 1_000;
    for (const height of heights) {
      const result = processDimensionChanges(
        nodes,
        [dimensionChange("a", 200, height)],
        state,
        { now },
      );
      if (result.shouldLayout) layoutCount += 1;
      now += DIMENSION_CONVERGENCE_QUIET_WINDOW_MS;
    }

    // Each resize sits a full quiet window apart, so none can be a feedback
    // loop: every one re-arms and lays out, never starving into stale positions.
    expect(layoutCount).toBe(heights.length);
  });

  test("a tight back-to-back loop stays bounded despite the quiet-window re-arm", () => {
    const nodes = [makeNode("a", 200, 100)];
    const state = createDimensionConvergenceState();

    let layoutCount = 0;
    let now = 1_000;
    for (let i = 0; i < 25; i += 1) {
      const height = i % 2 === 0 ? 120 : 160;
      const result = processDimensionChanges(
        nodes,
        [dimensionChange("a", 200, height)],
        state,
        { now },
      );
      if (result.shouldLayout) layoutCount += 1;
      // Changes arrive far faster than the quiet window, so the budget never
      // re-arms and the React #185 loop stays bounded.
      now += 50;
    }

    expect(layoutCount).toBeLessThanOrEqual(MAX_DIMENSION_LAYOUT_PASSES);
  });

  test("a genuine edit reset re-arms the pass budget", () => {
    const nodes = [makeNode("a", 200, 100)];
    const state = createDimensionConvergenceState();

    let height = 120;
    for (let i = 0; i < 10; i += 1) {
      height = i % 2 === 0 ? 120 : 160;
      processDimensionChanges(
        nodes,
        [dimensionChange("a", 200, height)],
        state,
      );
    }

    resetDimensionConvergence(state);

    const afterReset = processDimensionChanges(
      nodes,
      [dimensionChange("a", 200, 220)],
      state,
    );
    expect(afterReset.shouldLayout).toBe(true);
  });

  test("an intermittent resize after the layout settles is not starved", () => {
    const nodes = [makeNode("a", 200, 100)];
    const state = createDimensionConvergenceState();

    const grow = processDimensionChanges(
      nodes,
      [dimensionChange("a", 200, 140)],
      state,
    );
    const settle = processDimensionChanges(
      nodes,
      [dimensionChange("a", 200, 140)],
      state,
    );
    const growAgain = processDimensionChanges(
      nodes,
      [dimensionChange("a", 200, 180)],
      state,
    );

    expect(grow.shouldLayout).toBe(true);
    expect(settle.shouldLayout).toBe(false);
    expect(growAgain.shouldLayout).toBe(true);
  });

  test("a burst of distinct node resizes is never suppressed as a loop", () => {
    const ids = ["a", "b", "c", "d", "e"];
    const nodes = ids.map((id) => makeNode(id, 200, 100));
    const state = createDimensionConvergenceState();

    let layoutCount = 0;
    for (const id of ids) {
      const result = processDimensionChanges(
        nodes,
        [dimensionChange(id, 200, 140)],
        state,
      );
      if (result.shouldLayout) layoutCount += 1;
    }

    expect(layoutCount).toBe(ids.length);
  });

  test("a per-node budget bounds a loop without starving other nodes", () => {
    const nodes = [makeNode("loop", 200, 100), makeNode("calm", 200, 100)];
    const state = createDimensionConvergenceState();

    let loopLayouts = 0;
    for (let i = 0; i < 12; i += 1) {
      const result = processDimensionChanges(
        nodes,
        [dimensionChange("loop", 200, i % 2 === 0 ? 120 : 160)],
        state,
      );
      if (result.shouldLayout) loopLayouts += 1;
    }

    const calm = processDimensionChanges(
      nodes,
      [dimensionChange("calm", 200, 150)],
      state,
    );

    expect(loopLayouts).toBeLessThanOrEqual(MAX_DIMENSION_LAYOUT_PASSES);
    expect(calm.shouldLayout).toBe(true);
  });

  test("ignores dimension changes for unknown node ids", () => {
    const nodes = [makeNode("a", 200, 100)];
    const state = createDimensionConvergenceState();

    const result = processDimensionChanges(
      nodes,
      [dimensionChange("ghost", 300, 300)],
      state,
    );

    expect(result.shouldLayout).toBe(false);
  });
});
