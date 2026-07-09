import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRecordingStore } from "@/store/useRecordingStore";

import { useRecordingElapsedSeconds } from "./useRecordingElapsedSeconds";

const BASE_MS = 1_700_000_000_000;

describe("useRecordingElapsedSeconds", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(BASE_MS);
    useRecordingStore.setState({
      recordingStartedAtMs: BASE_MS,
      manualCapturePaused: false,
      finishRequested: false,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("counts up while recording", () => {
    const { result } = renderHook(() => useRecordingElapsedSeconds());
    expect(result.current).toBe(0);
    act(() => vi.advanceTimersByTime(3000));
    expect(Math.floor(result.current)).toBe(3);
  });

  it("freezes while paused and resumes without jumping the clock", () => {
    const { result } = renderHook(() => useRecordingElapsedSeconds());
    act(() => vi.advanceTimersByTime(3000));
    expect(Math.floor(result.current)).toBe(3);

    act(() => {
      useRecordingStore.setState({ manualCapturePaused: true });
    });
    act(() => vi.advanceTimersByTime(5000));
    // Frozen: the 5s spent paused must not count.
    expect(Math.floor(result.current)).toBe(3);

    act(() => {
      useRecordingStore.setState({ manualCapturePaused: false });
    });
    // Resume must not jump forward by the paused span.
    expect(Math.floor(result.current)).toBe(3);
    act(() => vi.advanceTimersByTime(2000));
    expect(Math.floor(result.current)).toBe(5);
  });
});
