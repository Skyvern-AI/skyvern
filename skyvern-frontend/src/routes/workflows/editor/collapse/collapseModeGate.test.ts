import { describe, expect, test } from "vitest";

import { isCollapseGated } from "./collapseModeGate";

describe("isCollapseGated (SKY-12246)", () => {
  test("returns false when nothing gates collapse", () => {
    expect(
      isCollapseGated({
        isRecording: false,
        isReadOnlyScope: false,
        isCanvasLocked: false,
      }),
    ).toBe(false);
  });

  test("returns true when recording is on", () => {
    expect(
      isCollapseGated({
        isRecording: true,
        isReadOnlyScope: false,
        isCanvasLocked: false,
      }),
    ).toBe(true);
  });

  test("returns true when scope is read-only", () => {
    expect(
      isCollapseGated({
        isRecording: false,
        isReadOnlyScope: true,
        isCanvasLocked: false,
      }),
    ).toBe(true);
  });

  test("returns true when canvas is locked", () => {
    expect(
      isCollapseGated({
        isRecording: false,
        isReadOnlyScope: false,
        isCanvasLocked: true,
      }),
    ).toBe(true);
  });
});
