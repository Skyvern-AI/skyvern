import { useEffect, useState } from "react";

import { useRecordingStore } from "@/store/useRecordingStore";

export function useRecordingElapsedSeconds(): number {
  const startedAtMs = useRecordingStore((state) => state.recordingStartedAtMs);
  const finishRequested = useRecordingStore((state) => state.finishRequested);

  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    if (finishRequested) {
      return;
    }
    setNowMs(Date.now());
    const interval = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [finishRequested]);

  if (!startedAtMs) {
    return 0;
  }
  return (nowMs - startedAtMs) / 1000;
}
