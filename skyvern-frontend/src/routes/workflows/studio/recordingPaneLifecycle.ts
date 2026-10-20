import { type StudioPaneId } from "./panes";

type RecordingPaneTransition = "started" | "processing" | "ended";

export function advanceRecordingStopLifecycle(
  finishWasRequested: boolean,
  state: {
    isRecording: boolean;
    wasRecording: boolean;
    finishRequested: boolean;
    processingRecording: boolean;
  },
): {
  finishWasRequested: boolean;
  transition: RecordingPaneTransition | null;
} {
  let nextFinishWasRequested =
    state.isRecording && !state.wasRecording ? false : finishWasRequested;
  if (state.finishRequested) {
    nextFinishWasRequested = true;
  }
  const transition =
    !state.isRecording && state.wasRecording && !state.processingRecording
      ? nextFinishWasRequested
        ? "processing"
        : "ended"
      : null;
  if (!state.isRecording && !state.processingRecording) {
    nextFinishWasRequested = false;
  }
  return { finishWasRequested: nextFinishWasRequested, transition };
}

export function panesAfterRecordingTransition(
  panes: readonly StudioPaneId[],
  transition: RecordingPaneTransition,
): StudioPaneId[] {
  const hadCopilot = panes.includes("copilot");
  const next = panes.filter((pane) => {
    if (transition === "started") {
      return pane !== "editor" && pane !== "copilot";
    }
    if (transition === "processing") {
      return pane !== "browser" && pane !== "copilot";
    }
    return pane !== "copilot";
  });
  const requiredPanes: StudioPaneId[] =
    transition === "started"
      ? ["browser", "copilot"]
      : ["editor", ...(hadCopilot ? (["copilot"] as const) : [])];

  for (const pane of requiredPanes) {
    if (!next.includes(pane)) {
      next.push(pane);
    }
  }

  return next;
}
