import { useEffect, useRef, useState } from "react";

import { useRecordingStore } from "@/store/useRecordingStore";

/**
 * Elapsed recording time in seconds, excluding any time spent manually paused.
 * The displayed clock freezes while `manualCapturePaused` is set and resumes
 * from where it left off — paused spans are folded into an accumulator rather
 * than counted, so resume never jumps the clock forward.
 */
export function useRecordingElapsedSeconds(): number {
  const startedAtMs = useRecordingStore((state) => state.recordingStartedAtMs);
  const paused = useRecordingStore((state) => state.manualCapturePaused);
  const finishRequested = useRecordingStore((state) => state.finishRequested);

  const [nowMs, setNowMs] = useState(() => Date.now());
  const pausedAccumMsRef = useRef(0);
  const pauseStartedAtMsRef = useRef<number | null>(null);

  // A new recording (new start timestamp) resets the paused accumulator.
  useEffect(() => {
    pausedAccumMsRef.current = 0;
    pauseStartedAtMsRef.current = null;
  }, [startedAtMs]);

  useEffect(() => {
    if (finishRequested) {
      return;
    }

    if (paused) {
      // Freeze: mark when the pause began and stop ticking.
      if (pauseStartedAtMsRef.current === null) {
        pauseStartedAtMsRef.current = Date.now();
        setNowMs(Date.now());
      }
      return;
    }

    // Resuming: fold the just-ended pause into the accumulator so it is not
    // counted, then keep ticking.
    if (pauseStartedAtMsRef.current !== null) {
      pausedAccumMsRef.current += Date.now() - pauseStartedAtMsRef.current;
      pauseStartedAtMsRef.current = null;
    }
    setNowMs(Date.now());
    const interval = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [paused, finishRequested]);

  if (!startedAtMs) {
    return 0;
  }
  return (nowMs - startedAtMs - pausedAccumMsRef.current) / 1000;
}
