// @vitest-environment jsdom

import { renderHook } from "@testing-library/react";
import type { Blocker } from "react-router-dom";
import { describe, expect, test, vi } from "vitest";

import { useBlockerExit } from "./useBlockerExit";

function blocked(proceed: () => void, reset: () => void): Blocker {
  return {
    state: "blocked",
    proceed,
    reset,
    location: { pathname: "/next" },
  } as unknown as Blocker;
}

function unblocked(): Blocker {
  return {
    state: "unblocked",
    proceed: undefined,
    reset: undefined,
    location: undefined,
  } as unknown as Blocker;
}

describe("useBlockerExit", () => {
  test("proceeds on a blocked episode", () => {
    const proceed = vi.fn();
    const { result } = renderHook(() =>
      useBlockerExit(blocked(proceed, vi.fn())),
    );

    result.current.proceed();

    expect(proceed).toHaveBeenCalledTimes(1);
  });

  // The router advances synchronously but React re-renders later, so a callback
  // resuming in between still holds a snapshot reading "blocked". Not re-rendering
  // between the two calls is what reproduces that window.
  test("does not proceed twice while the snapshot still reads blocked", () => {
    const proceed = vi.fn();
    const { result } = renderHook(() =>
      useBlockerExit(blocked(proceed, vi.fn())),
    );

    result.current.proceed();
    result.current.proceed();

    expect(proceed).toHaveBeenCalledTimes(1);
  });

  // SKY-13124: save in flight, user dismisses the dialog (Escape/overlay) so reset
  // moves the router to unblocked, then the save resolves and proceeds into it.
  test("does not proceed after the episode was already reset", () => {
    const proceed = vi.fn();
    const reset = vi.fn();
    const { result } = renderHook(() =>
      useBlockerExit(blocked(proceed, reset)),
    );

    result.current.reset();
    result.current.proceed();

    expect(reset).toHaveBeenCalledTimes(1);
    expect(proceed).not.toHaveBeenCalled();
  });

  test("proceeds again on a later blocking episode", () => {
    const firstProceed = vi.fn();
    const nextProceed = vi.fn();
    const { result, rerender } = renderHook(
      ({ blocker }: { blocker: Blocker }) => useBlockerExit(blocker),
      { initialProps: { blocker: blocked(firstProceed, vi.fn()) } },
    );

    result.current.proceed();
    rerender({ blocker: unblocked() });
    rerender({ blocker: blocked(nextProceed, vi.fn()) });
    result.current.proceed();

    expect(firstProceed).toHaveBeenCalledTimes(1);
    expect(nextProceed).toHaveBeenCalledTimes(1);
  });
});
