import _GestureHandler from "@novnc/novnc/lib/input/gesturehandler.js";

const GestureHandler =
  (
    _GestureHandler as typeof _GestureHandler & {
      default?: typeof _GestureHandler;
    }
  ).default ?? _GestureHandler;

let installed = false;

export function installNoVncGestureCrashGuard(): void {
  if (installed) return;
  installed = true;

  const originalTouchEnd = GestureHandler.prototype._touchEnd;
  GestureHandler.prototype._touchEnd = function (
    id: number,
    x: number,
    y: number,
  ): void {
    const isTracked = this._tracked.some((touch) => touch.id === id);
    const isIgnored = this._ignored.includes(id);
    // noVNC 1.5.0 dereferences the lookup even when a touch ends after the handler attached mid-gesture.
    if (!isTracked && !isIgnored) return;

    originalTouchEnd.call(this, id, x, y);
  };
}
