// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { scheduleCollapseRelayout } from "./scheduleCollapseRelayout";

const EVENT = "conditional-header-resized" as const;

// Manual rAF queue so the two-frame deferral is driven explicitly instead of
// racing real timers. Handles are monotonic ids (never reused) so a cancel
// targets exactly the frame it was issued for.
let rafCallbacks: Map<number, FrameRequestCallback>;
let nextRafHandle: number;

function flushFrame() {
  const callbacks = rafCallbacks;
  rafCallbacks = new Map();
  callbacks.forEach((cb) => cb(performance.now()));
}

beforeEach(() => {
  rafCallbacks = new Map();
  nextRafHandle = 1;
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb) => {
    const handle = nextRafHandle++;
    rafCallbacks.set(handle, cb);
    return handle;
  });
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation((handle) => {
    rafCallbacks.delete(handle);
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

function listenOnce() {
  const handler = vi.fn();
  window.addEventListener(EVENT, handler);
  return {
    handler,
    cleanup: () => window.removeEventListener(EVENT, handler),
  };
}

describe("scheduleCollapseRelayout", () => {
  test("expanding dispatches the re-layout event synchronously", () => {
    const { handler, cleanup } = listenOnce();
    scheduleCollapseRelayout(EVENT, true, false);
    expect(handler).toHaveBeenCalledTimes(1);
    cleanup();
  });

  test("collapsing dispatches only after two animation frames", () => {
    const { handler, cleanup } = listenOnce();
    scheduleCollapseRelayout(EVENT, false, true);

    expect(handler).not.toHaveBeenCalled();
    flushFrame();
    expect(handler).not.toHaveBeenCalled();
    flushFrame();
    expect(handler).toHaveBeenCalledTimes(1);
    cleanup();
  });

  test("the collapse cleanup cancels a still-pending dispatch", () => {
    const { handler, cleanup } = listenOnce();
    const dispose = scheduleCollapseRelayout(EVENT, false, true);

    flushFrame(); // first frame scheduled the second
    dispose(); // unmount / effect re-run before the second frame fires
    flushFrame();

    expect(handler).not.toHaveBeenCalled();
    cleanup();
  });

  test("disposing before the first frame fires cancels the outer frame and never dispatches", () => {
    const { handler, cleanup } = listenOnce();
    const dispose = scheduleCollapseRelayout(EVENT, false, true);

    dispose(); // unmount before either frame runs
    flushFrame();
    flushFrame();

    expect(handler).not.toHaveBeenCalled();
    cleanup();
  });

  test("initial mount in the collapsed state does not dispatch", () => {
    const { handler, cleanup } = listenOnce();
    scheduleCollapseRelayout(EVENT, null, true);

    flushFrame();
    flushFrame();
    expect(handler).not.toHaveBeenCalled();
    cleanup();
  });

  test("a no-op re-render (still expanded) does not dispatch", () => {
    const { handler, cleanup } = listenOnce();
    scheduleCollapseRelayout(EVENT, false, false);

    flushFrame();
    flushFrame();
    expect(handler).not.toHaveBeenCalled();
    cleanup();
  });
});
