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
});
