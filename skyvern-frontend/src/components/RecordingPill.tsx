import { useMemo } from "react";
import { useShallow } from "zustand/react/shallow";

import { useRecordingElapsedSeconds } from "@/hooks/useRecordingElapsedSeconds";
import {
  useRecordingStore,
  countVisibleDraftSteps,
} from "@/store/useRecordingStore";
import { formatRecordingClock } from "@/util/recordingClock";
import { cn } from "@/util/utils";

export function RecordingPill() {
  const {
    finishRequested,
    draftSteps,
    deletedStepIds,
    exposedEventCount,
    optimisticStepCount,
    interpretationEnabled,
  } = useRecordingStore(
    useShallow((state) => ({
      finishRequested: state.finishRequested,
      draftSteps: state.draftSteps,
      deletedStepIds: state.deletedStepIds,
      exposedEventCount: state.exposedEventCount,
      optimisticStepCount: state.optimisticSteps.length,
      interpretationEnabled: state.workflowPermanentId !== null,
    })),
  );

  const interpretedStepCount = useMemo(
    () => countVisibleDraftSteps(draftSteps, deletedStepIds),
    [draftSteps, deletedStepIds],
  );
  const elapsedSeconds = useRecordingElapsedSeconds();
  // Show interpreted + optimistic steps whenever interpretation is enabled, not
  // just after the first snapshot arrives — otherwise the first step waits a
  // backend round-trip even though the optimistic placeholder is already local.
  const count = interpretationEnabled
    ? interpretedStepCount + optimisticStepCount
    : exposedEventCount;

  return (
    <div
      data-testid="recording-pill"
      className={cn(
        "inline-flex h-6 items-center gap-2 rounded-full border px-3 text-xs font-semibold tabular-nums",
        "border-red-500/50 bg-red-950 text-red-200",
      )}
    >
      <span
        className={cn("h-2 w-2 rounded-full bg-red-500", {
          "animate-pulse": !finishRequested,
          "opacity-50": finishRequested,
        })}
      />
      {finishRequested ? "CREATING STEPS" : "RECORDING TASK"}{" "}
      {formatRecordingClock(elapsedSeconds)}
      <span className="text-red-400/80">·</span>
      {count}
    </div>
  );
}
