import { useEffect, useRef } from "react";
import { create } from "zustand";

import { useSettingsStore } from "@/store/SettingsStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import {
  useWorkflowYamlEditorStore,
  type YamlCommitOwner,
} from "@/store/WorkflowYamlEditorStore";
import { captureRecordBrowser } from "@/util/recordBrowserTelemetry";

type RecordingEntryPoint = "launcher" | "edge" | "node_adder" | "auto_record";

export const usePendingRecordingStartGate = create<{
  // Returns whether the recording started.
  blockedStart: (() => boolean) | null;
  // Save handed off to the code-cache confirmation; start when that save lands.
  awaitingConfirmedSave: boolean;
  // Discard rebuilds the graph, so only a start that recomputes its location
  // (the append-at-end launcher) can follow it.
  discardRecords: boolean;
  // The editor that asked; the prompt belongs to it alone.
  owner: YamlCommitOwner | null;
}>(() => ({
  blockedStart: null,
  awaitingConfirmedSave: false,
  discardRecords: true,
  owner: null,
}));

// Every Record Browser entry point starts through here: processing refuses a
// recording while generated changes are pending, so asking afterwards would
// waste the new demonstration.
export function requestRecordingStart(
  start: () => void,
  entryPoint: RecordingEntryPoint,
  {
    isStillValid,
    recordsAfterDiscard = true,
  }: {
    // Re-checked when a deferred start fires, e.g. browser readiness or a
    // canvas location that still exists.
    isStillValid?: () => boolean;
    // False for a start bound to canvas ids that Discard regenerates.
    recordsAfterDiscard?: boolean;
  } = {},
): void {
  const changes = useWorkflowHasChangesStore.getState();
  const { pendingRecordingId, pendingRecordingWorkflowPermanentId } = changes;
  if (pendingRecordingId === null) return start();
  // A marker left by a workflow this tab no longer edits has no draft to save.
  const editor = useWorkflowYamlEditorStore.getState();
  if (
    pendingRecordingWorkflowPermanentId !==
    editor.editorOwner?.workflowPermanentId
  ) {
    // A persisting save has already put this recording id in its request.
    if (
      pendingRecordingWorkflowPermanentId &&
      editor.pendingSaves[pendingRecordingWorkflowPermanentId]?.persisting
    )
      changes.clearPendingRecording(pendingRecordingId);
    else changes.discardPendingRecording(pendingRecordingId);
    return start();
  }
  // The save can outlive this editor or its browser; only start where it was asked for.
  const owner = useWorkflowYamlEditorStore.getState().editorOwner;
  const browserSessionId = useSettingsStore.getState().browserSessionId;
  // No recording attempt exists yet; don't inherit the previous attempt's id.
  captureRecordBrowser("record_browser.start_blocked_pending_changes", {
    entry_point: entryPoint,
    recording_attempt_id: undefined,
    workflow_permanent_id: owner?.workflowPermanentId,
    browser_session_id: browserSessionId ?? undefined,
  });
  usePendingRecordingStartGate.setState({
    blockedStart: () => {
      const current = useWorkflowYamlEditorStore.getState().editorOwner;
      if (
        current !== owner ||
        !current?.active ||
        useSettingsStore.getState().browserSessionId !== browserSessionId ||
        isStillValid?.() === false
      )
        return false;
      start();
      return true;
    },
    awaitingConfirmedSave: false,
    discardRecords: recordsAfterDiscard,
    owner,
  });
}

// Discard rebuilds the graph with new node ids, unmounting the canvas location
// a deferred start would insert at.
export function useIsMountedRef() {
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  return mounted;
}
