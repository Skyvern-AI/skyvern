import { describe, expect, test } from "vitest";

import { getDragGateReason, isDragGatedByMode } from "./dragModeGate";

describe("isDragGatedByMode (SKY-9061)", () => {
  test("returns false when nothing gates drag", () => {
    expect(isDragGatedByMode({ isCanvasLocked: false })).toBe(false);
  });

  test("returns true when canvas is locked", () => {
    expect(isDragGatedByMode({ isCanvasLocked: true })).toBe(true);
  });
});

describe("getDragGateReason (SKY-9061)", () => {
  test("returns null when no gate is active", () => {
    expect(getDragGateReason({ isCanvasLocked: false })).toBeNull();
  });

  test("returns the canvas-lock hint when canvas is locked", () => {
    expect(getDragGateReason({ isCanvasLocked: true })).toBe(
      "Unlock canvas to reorder blocks",
    );
  });
});
