import { describe, expect, test } from "vitest";

import { getDragGateReason, isDragGatedByMode } from "./dragModeGate";

describe("isDragGatedByMode (SKY-9061)", () => {
  test("returns false when nothing gates drag", () => {
    expect(
      isDragGatedByMode({ isRecording: false, isCanvasLocked: false }),
    ).toBe(false);
  });

  test("returns true when recording is on", () => {
    expect(
      isDragGatedByMode({ isRecording: true, isCanvasLocked: false }),
    ).toBe(true);
  });

  test("returns true when canvas is locked", () => {
    expect(
      isDragGatedByMode({ isRecording: false, isCanvasLocked: true }),
    ).toBe(true);
  });
});

describe("getDragGateReason (SKY-9061)", () => {
  test("returns null when no gate is active", () => {
    expect(
      getDragGateReason({ isRecording: false, isCanvasLocked: false }),
    ).toBeNull();
  });

  test("returns the recording hint when recording is on", () => {
    expect(
      getDragGateReason({ isRecording: true, isCanvasLocked: false }),
    ).toBe("Stop recording to reorder blocks");
  });

  test("returns the canvas-lock hint when only canvas is locked", () => {
    expect(
      getDragGateReason({ isRecording: false, isCanvasLocked: true }),
    ).toBe("Unlock canvas to reorder blocks");
  });

  test("prefers recording hint when both gates are active", () => {
    expect(getDragGateReason({ isRecording: true, isCanvasLocked: true })).toBe(
      "Stop recording to reorder blocks",
    );
  });
});
