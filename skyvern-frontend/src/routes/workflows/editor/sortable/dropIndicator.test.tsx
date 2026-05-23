// @vitest-environment jsdom

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { deriveDropIndicator } from "./dropIndicator";

/**
 * SKY-9068 — insertion-line indicator tests.
 *
 * Covers the two surfaces that land in the same commit:
 *   - `deriveDropIndicator` (pure) — the index-comparison logic that maps
 *     (scope order, active, over) to the indicator state. The wire in
 *     FlowRenderer delegates to this helper, so covering every branch here
 *     gates every code path that ultimately shows / hides the line.
 *   - `DropPositionIndicator` (render) — reads React Flow's node lookup +
 *     viewport transform via `useStore`. Mocked here rather than mounting a
 *     full ReactFlow instance: the component's contract is "render a 2 px
 *     line at the right place given a known rect", and we don't want this
 *     test to regress when unrelated RF internals shift.
 */

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.resetModules();
});

describe("deriveDropIndicator", () => {
  const order = ["a", "b", "c"];

  test("returns null when active === over (drop-on-self no-op)", () => {
    expect(deriveDropIndicator({ order, activeId: "b", overId: "b" })).toBe(
      null,
    );
  });

  test("returns null when active id is not in the scope order", () => {
    // Dragging a block whose scope doesn't contain it (shouldn't happen in
    // practice, but the helper is the only guard for malformed calls).
    expect(deriveDropIndicator({ order, activeId: "x", overId: "b" })).toBe(
      null,
    );
  });

  test("returns null when over id is not in the scope order (cross-scope)", () => {
    // Cross-scope hover: active is a top-level block, over is a loop child.
    // The scope's `order` only contains top-level ids, so `over` is absent
    // and we must hide the indicator — the existing classifier will refuse
    // the drop anyway, but the indicator must not point at a slot the drop
    // can't land in.
    expect(
      deriveDropIndicator({ order, activeId: "a", overId: "nested" }),
    ).toBe(null);
  });

  test('placement is "below" when dragging downward (active < over)', () => {
    // active at index 0, over at index 2 → arrayMove would splice active in
    // AFTER the over slot → line belongs below the over block.
    expect(deriveDropIndicator({ order, activeId: "a", overId: "c" })).toEqual({
      overId: "c",
      placement: "below",
    });
  });

  test('placement is "above" when dragging upward (active > over)', () => {
    // active at index 2, over at index 0 → arrayMove would splice active in
    // BEFORE the over slot → line belongs above the over block.
    expect(deriveDropIndicator({ order, activeId: "c", overId: "a" })).toEqual({
      overId: "a",
      placement: "above",
    });
  });

  test("adjacent blocks resolve to the correct side", () => {
    // The common case — dragging onto the immediate neighbour. Covered
    // separately because it's where off-by-one bugs hide (an off-by-one in
    // the `<` would flip above/below for adjacent blocks only).
    expect(deriveDropIndicator({ order, activeId: "a", overId: "b" })).toEqual({
      overId: "b",
      placement: "below",
    });
    expect(deriveDropIndicator({ order, activeId: "b", overId: "a" })).toEqual({
      overId: "a",
      placement: "above",
    });
  });
});

describe("DropPositionIndicator", () => {
  /**
   * Build a minimal RF-store shape (nodeLookup + transform) and install a
   * `useStore` mock that selects from it. This is enough for the component
   * under test: it only reads `s.nodeLookup.get(id)` and `s.transform`, so
   * covering those two slices keeps the test independent from the rest of
   * RF's internal store shape.
   */
  async function renderIndicator({
    state,
    nodeId,
    nodePosition,
    nodeSize,
    transform,
  }: {
    state: { overId: string; placement: "above" | "below" } | null;
    nodeId: string;
    nodePosition: { x: number; y: number };
    nodeSize: { width: number; height: number };
    transform: [number, number, number];
  }) {
    const nodeLookup = new Map<string, unknown>([
      [
        nodeId,
        {
          position: nodePosition,
          measured: nodeSize,
          internals: { positionAbsolute: nodePosition },
        },
      ],
    ]);
    vi.doMock("@xyflow/react", () => {
      type Selector<T> = (s: {
        nodeLookup: Map<string, unknown>;
        transform: [number, number, number];
      }) => T;
      return {
        useStore: <T,>(selector: Selector<T>): T =>
          selector({ nodeLookup, transform }),
      };
    });
    // Import lazily after the mock is in place — re-importing the module
    // under test is the only reliable way to make vi.doMock take effect in
    // a Vitest test file with static imports elsewhere.
    const { DropPositionIndicator } = await import("./DropPositionIndicator");
    return render(<DropPositionIndicator state={state} />);
  }

  test("renders nothing when state is null", async () => {
    // The happy path for "drag is over nothing" or "cross-scope hover": the
    // component must gracefully render null rather than a zero-size line.
    await renderIndicator({
      state: null,
      nodeId: "ignored",
      nodePosition: { x: 0, y: 0 },
      nodeSize: { width: 100, height: 40 },
      transform: [0, 0, 1],
    });
    expect(
      document.querySelector('[data-testid="drop-position-indicator"]'),
    ).toBeNull();
  });

  test('renders at rect.top + rect.height - 1 for placement "below"', async () => {
    // zoom = 1, no pan → node at flow (50, 100) with 120x30 measured size →
    // screen rect (50, 100, 120, 30). placement "below" anchors the line at
    // rect.top + rect.height - 1 = 129.
    await renderIndicator({
      state: { overId: "b", placement: "below" },
      nodeId: "b",
      nodePosition: { x: 50, y: 100 },
      nodeSize: { width: 120, height: 30 },
      transform: [0, 0, 1],
    });
    const line = document.querySelector<HTMLElement>(
      '[data-testid="drop-position-indicator"]',
    );
    if (!line) throw new Error("indicator not rendered");
    expect(line.getAttribute("data-placement")).toBe("below");
    expect(line.getAttribute("data-over-id")).toBe("b");
    expect(line.style.left).toBe("50px");
    expect(line.style.top).toBe("129px");
    expect(line.style.width).toBe("120px");
    expect(line.style.height).toBe("2px");
    // pointer-events must be off so the line doesn't interrupt drag tracking.
    expect(line.style.pointerEvents).toBe("none");
  });

  test('renders at rect.top - 1 for placement "above"', async () => {
    // Same rect, placement flipped. Anchors the line at rect.top - 1 = 99.
    await renderIndicator({
      state: { overId: "b", placement: "above" },
      nodeId: "b",
      nodePosition: { x: 50, y: 100 },
      nodeSize: { width: 120, height: 30 },
      transform: [0, 0, 1],
    });
    const line = document.querySelector<HTMLElement>(
      '[data-testid="drop-position-indicator"]',
    );
    if (!line) throw new Error("indicator not rendered");
    expect(line.getAttribute("data-placement")).toBe("above");
    expect(line.style.top).toBe("99px");
  });

  test("applies viewport transform (pan + zoom) to the indicator rect", async () => {
    // Pan (10, 20) with zoom 2 on a node at flow (50, 100) sized 100x40:
    // screen left = 50*2 + 10 = 110, top = 100*2 + 20 = 220,
    // width = 100*2 = 200, height = 40*2 = 80.
    // placement "below" → line top = 220 + 80 - 1 = 299.
    await renderIndicator({
      state: { overId: "b", placement: "below" },
      nodeId: "b",
      nodePosition: { x: 50, y: 100 },
      nodeSize: { width: 100, height: 40 },
      transform: [10, 20, 2],
    });
    const line = document.querySelector<HTMLElement>(
      '[data-testid="drop-position-indicator"]',
    );
    if (!line) throw new Error("indicator not rendered");
    expect(line.style.left).toBe("110px");
    expect(line.style.top).toBe("299px");
    expect(line.style.width).toBe("200px");
  });

  test("collapsed-block rect: indicator anchors to measured.height (SKY-9069)", async () => {
    // A collapsed AppNode's DOM reports a much smaller height than the
    // expanded card (~72 px vs. several hundred). The indicator reads
    // `measured.height` via `useNodeScreenRect`, so feeding in the
    // collapsed size must place the "below" line at collapsed_top +
    // collapsed_height - 1 — NOT at the stale expanded value. This is the
    // integration point between #10533 (drop indicator) and SKY-9069
    // (compact blocks); a regression here would draw the line below the
    // expanded bottom while the card itself sits above.
    await renderIndicator({
      state: { overId: "collapsed-block", placement: "below" },
      nodeId: "collapsed-block",
      nodePosition: { x: 50, y: 100 },
      // ~30rem * 16 = 480 px wide; header-only collapse height ~= 72 px.
      nodeSize: { width: 480, height: 72 },
      transform: [0, 0, 1],
    });
    const line = document.querySelector<HTMLElement>(
      '[data-testid="drop-position-indicator"]',
    );
    if (!line) throw new Error("indicator not rendered");
    expect(line.style.top).toBe("171px"); // 100 + 72 - 1
    expect(line.style.width).toBe("480px");
  });

  test("renders nothing when the over node is missing from the RF store", async () => {
    // Guards against a race where dnd-kit emits onDragOver for an id that
    // RF hasn't measured yet (e.g. a node that's still in its pre-layout
    // phase). Silent null is better than throwing.
    await renderIndicator({
      state: { overId: "ghost", placement: "below" },
      nodeId: "b", // only "b" is in the lookup; "ghost" is not
      nodePosition: { x: 50, y: 100 },
      nodeSize: { width: 100, height: 40 },
      transform: [0, 0, 1],
    });
    expect(
      document.querySelector('[data-testid="drop-position-indicator"]'),
    ).toBeNull();
  });
});
