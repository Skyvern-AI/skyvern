import { useEffect, useRef } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import { useRecordingStore } from "@/store/useRecordingStore";
import { useRunViewStore } from "@/store/RunViewStore";
import {
  useStudioBrowserStore,
  type BrowserPaneViewIntent,
} from "@/store/useStudioBrowserStore";

import { useDebugSessionQuery } from "../hooks/useDebugSessionQuery";
import {
  resolveBrowserPaneView,
  type BrowserPaneView,
} from "./browserPaneView";
import { useRunVisuals, type RunVisuals } from "./useRunVisuals";
import { useStudioInspectedRun } from "./useStudioInspectedRun";

type BrowserPaneViewState = {
  view: BrowserPaneView;
  setView: (view: BrowserPaneViewIntent) => void;
  visuals: RunVisuals;
  runId: string | undefined;
  debugBrowserSessionId: string | null;
  // The inspected run executes in the live debug session, so its live view is
  // the shared singleton stream — never a second socket to the same browser.
  runInDebugSession: boolean;
  // What the Live view shows: the shared debug-session singleton, or the
  // inspected run's own per-run stream (running outside the debug session).
  liveSurface: "debug" | "run";
};

/**
 * Run-aware Browser pane state: which of Live / Recording / Screenshots the
 * pane shows, resolved from the pill intent and the inspected run. Shared by
 * the pane body and its header chrome (queries dedupe via react-query).
 */
export function useBrowserPaneView(): BrowserPaneViewState {
  const { workflowPermanentId } = useParams();
  const [searchParams] = useSearchParams();
  const { runId, explicit } = useStudioInspectedRun();
  const visuals = useRunVisuals(runId);
  const { data: debugSession } = useDebugSessionQuery({
    workflowPermanentId,
    enabled: false,
  });
  const debugBrowserSessionId = debugSession?.browser_session_id ?? null;
  const intent = useStudioBrowserStore((s) => s.view);
  const setView = useStudioBrowserStore((s) => s.setView);
  const pinNonce = useRunViewStore((s) => s.pinNonce);
  const isRecording = useRecordingStore((s) => s.isRecording);

  // Timeline selection outranks a pinned pill: a step click (?active= change or
  // a re-pin of the same step) or a run swap hands the pane back to the machine,
  // which lands on Screenshots for a pinned step and Live for a fresh run.
  const activeParam = searchParams.get("active");
  const syncRef = useRef({ activeParam, pinNonce, runId, isRecording });
  useEffect(() => {
    const prev = syncRef.current;
    const recordingStarted = isRecording && !prev.isRecording;
    if (
      prev.activeParam !== activeParam ||
      prev.pinNonce !== pinNonce ||
      prev.runId !== runId ||
      recordingStarted
    ) {
      syncRef.current = { activeParam, pinNonce, runId, isRecording };
      setView("auto");
    } else if (prev.isRecording !== isRecording) {
      syncRef.current = { activeParam, pinNonce, runId, isRecording };
    }
  }, [activeParam, pinNonce, runId, isRecording, setView]);

  const runInDebugSession =
    visuals.workflowRun?.browser_session_id != null &&
    visuals.workflowRun.browser_session_id === debugBrowserSessionId;
  const blockRunInDebugSession = searchParams.has("bl") && runInDebugSession;

  const view = resolveBrowserPaneView({
    intent,
    recording: isRecording,
    scrubbing: visuals.scrubbing,
    inspectingRun: explicit,
    blockRunInDebugSession,
    running: visuals.running,
    hasRecording: visuals.recordingUrls.length > 0,
    failed: visuals.failed,
  });

  // Recording pins the live surface to the debug browser (the recorder drives
  // it), even if an inspected run happens to be streaming elsewhere.
  const liveSurface: "debug" | "run" =
    !isRecording && visuals.running && !runInDebugSession && runId != null
      ? "run"
      : "debug";

  return {
    view,
    setView,
    visuals,
    runId,
    debugBrowserSessionId,
    runInDebugSession,
    liveSurface,
  };
}
