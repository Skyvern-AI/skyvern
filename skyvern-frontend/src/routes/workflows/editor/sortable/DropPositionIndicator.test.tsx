// @vitest-environment jsdom

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

vi.mock("@xyflow/react", () => {
  const store = {
    nodeLookup: new Map([
      [
        "loop_a",
        {
          measured: { width: 800, height: 200 },
          internals: { positionAbsolute: { x: 0, y: 0 } },
          position: { x: 0, y: 0 },
        },
      ],
      [
        "block_b",
        {
          measured: { width: 320, height: 60 },
          internals: { positionAbsolute: { x: 50, y: 100 } },
          position: { x: 50, y: 100 },
        },
      ],
    ]),
    transform: [0, 0, 1] as const,
  };
  return {
    useStore: <T,>(selector: (s: typeof store) => T): T => selector(store),
  };
});

import { DropPositionIndicator } from "./DropPositionIndicator";

afterEach(() => {
  cleanup();
});

const INNER_BLOCK_MAX_PX = 30 * 16;

describe("DropPositionIndicator width clamp", () => {
  test("clamps width to ~30rem when the over node is wider (loop / conditional container)", () => {
    const { getByTestId } = render(
      <DropPositionIndicator
        state={{ overId: "loop_a", placement: "above" }}
      />,
    );
    const indicator = getByTestId("drop-position-indicator") as HTMLElement;
    const widthPx = parseInt(indicator.style.width, 10);
    expect(widthPx).toBeLessThanOrEqual(INNER_BLOCK_MAX_PX);
  });

  test("preserves narrower widths (regular block ≤ 30rem)", () => {
    const { getByTestId } = render(
      <DropPositionIndicator
        state={{ overId: "block_b", placement: "below" }}
      />,
    );
    const indicator = getByTestId("drop-position-indicator") as HTMLElement;
    const widthPx = parseInt(indicator.style.width, 10);
    expect(widthPx).toBe(320);
  });
});
