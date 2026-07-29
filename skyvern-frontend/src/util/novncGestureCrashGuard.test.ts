import { beforeAll, describe, expect, it } from "vitest";
import _GestureHandler from "@novnc/novnc/lib/input/gesturehandler.js";

import { installNoVncGestureCrashGuard } from "./novncGestureCrashGuard";

const GestureHandler =
  (
    _GestureHandler as typeof _GestureHandler & {
      default?: typeof _GestureHandler;
    }
  ).default ?? _GestureHandler;

describe("installNoVncGestureCrashGuard", () => {
  beforeAll(() => {
    installNoVncGestureCrashGuard();
  });

  it("ignores a touchend without a matching tracked touch", () => {
    const handler = new GestureHandler();

    expect(() => handler._touchEnd(1, 0, 0)).not.toThrow();
  });

  it("preserves normal tracked touch handling", () => {
    const handler = new GestureHandler();
    const target = document.createElement("div");

    handler.attach(target);
    handler._touchStart(1, 0, 0);
    expect(() => handler._touchEnd(1, 0, 0)).not.toThrow();
  });

  it("preserves ignored-touch cleanup", () => {
    const handler = new GestureHandler();
    handler._ignored.push(1);

    handler._touchEnd(1, 0, 0);

    expect(handler._ignored).not.toContain(1);
  });

  it("does not wrap the handler more than once", () => {
    const guardedTouchEnd = GestureHandler.prototype._touchEnd;

    installNoVncGestureCrashGuard();

    expect(GestureHandler.prototype._touchEnd).toBe(guardedTouchEnd);
  });
});
