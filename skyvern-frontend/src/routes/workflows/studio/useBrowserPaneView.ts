import { useCallback, useEffect, useRef } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";

import { useRecordingLauncherStore } from "@/store/useRecordingLauncherStore";
import { useRunViewStore } from "@/store/RunViewStore";
import {
  useStudioBrowserStore,
  type BrowserPaneViewIntent,
} from "@/store/useStudioBrowserStore";

import { useDebugSessionQuery } from "../hooks/useDebugSessionQuery";
import {
  resolveBrowserPaneView,
  resolveReplayAvailability,
  resolveLiveSurface,
  type BrowserPaneView,
} from "./browserPaneView";
import { SYSTEM_RUN_FOCUS_PARAM, toReadableSearch } from "./panes";
import { useRunVisuals, type RunVisuals } from "./useRunVisuals";
import { useStudioInspectedRun } from "./useStudioInspectedRun";
import { useStudioPanes } from "./useStudioPanes";

type BrowserPaneViewState = {
  view: BrowserPaneView;
  setView: (view: BrowserPaneViewIntent) => void;
  visuals: RunVisuals;
  runId: string | undefined;
  // The URL names the run, so the pane is inspecting it rather than falling
  // back to the workflow's latest run in the edit context.
  inspectingRun: boolean;
  debugBrowserSessionId: string | null;
  // The inspected run executes in the live debug session, so its live view is
  // the shared singleton stream — never a second socket to the same browser.
  runInDebugSession: boolean;
  // What the Live view shows: the shared debug-session singleton, or the
  // inspected run's own per-run stream (running outside the debug session).
  liveSurface: "debug" | "run";
  recordingAvailable: boolean;
  screenshotsAvailable: boolean;
  liveAvailable: boolean;
};

/**
 * Run-aware Browser pane state: which of Live / Recording / Screenshots the
 * pane shows, resolved from the pill intent and the inspected run. Shared by
 * the pane body and its header chrome (queries dedupe via react-query).
 */
export function useBrowserPaneView(): BrowserPaneViewState {
  const workflowPermanentId = useWorkflowPermanentId();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { openPane, resolveLivePanes } = useStudioPanes();
  const { runId, explicit } = useStudioInspectedRun();
  const visuals = useRunVisuals(runId);
  const { data: debugSession } = useDebugSessionQuery({
    workflowPermanentId,
    enabled: false,
  });
  const debugBrowserSessionId = debugSession?.browser_session_id ?? null;
  const intent = useStudioBrowserStore((s) => s.view);
  const setViewIntent = useStudioBrowserStore((s) => s.setView);
  const pinNonce = useRunViewStore((s) => s.pinNonce);
  const demonstrationBrowserSessionId = useRecordingLauncherStore(
    (s) => s.demonstrationBrowserSessionId,
  );
  const userDrivingDebugBrowser =
    demonstrationBrowserSessionId !== null &&
    demonstrationBrowserSessionId === debugBrowserSessionId;
  const recordingDeepLink = searchParams.get("view") === "recording";

  // Timeline selection outranks a pinned pill: a step click (?active= change or
  // a re-pin of the same step) or a run swap hands the pane back to the machine,
  // which lands on Screenshots for a pinned step and Live for a fresh run.
  const activeParam = searchParams.get("active");
  const syncRef = useRef({
    activeParam,
    pinNonce,
    runId,
    userDrivingDebugBrowser,
  });
  useEffect(() => {
    const prev = syncRef.current;
    const manualControlStarted =
      userDrivingDebugBrowser && !prev.userDrivingDebugBrowser;
    if (
      prev.activeParam !== activeParam ||
      prev.pinNonce !== pinNonce ||
      prev.runId !== runId ||
      manualControlStarted
    ) {
      if (!recordingDeepLink) {
        setViewIntent("auto");
      }
    }
    syncRef.current = { activeParam, pinNonce, runId, userDrivingDebugBrowser };
  }, [
    activeParam,
    pinNonce,
    runId,
    userDrivingDebugBrowser,
    recordingDeepLink,
    setViewIntent,
  ]);

  const appliedRecordingDeepLinkRef = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!recordingDeepLink) {
      appliedRecordingDeepLinkRef.current = undefined;
      return;
    }
    if (!runId || appliedRecordingDeepLinkRef.current === runId) {
      return;
    }
    appliedRecordingDeepLinkRef.current = runId;
    setViewIntent("recording");
  }, [recordingDeepLink, runId, setViewIntent]);

  const setView = useCallback(
    (nextView: BrowserPaneViewIntent) => {
      setViewIntent(nextView);
      const next = new URLSearchParams(searchParams);
      if (nextView === "recording") {
        next.set("view", "recording");
        if (!resolveLivePanes().includes("browser")) openPane("browser");
      } else if (next.get("view") === "recording") {
        next.delete("view");
      }
      navigate({ search: toReadableSearch(next) }, { replace: true });
    },
    [navigate, openPane, resolveLivePanes, searchParams, setViewIntent],
  );

  const runInDebugSession =
    visuals.workflowRun?.browser_session_id != null &&
    visuals.workflowRun.browser_session_id === debugBrowserSessionId;
  const blockRunInDebugSession = searchParams.has("bl") && runInDebugSession;

  const { recordingAvailable, screenshotsAvailable } =
    resolveReplayAvailability(visuals);

  const view = resolveBrowserPaneView({
    intent,
    recording: userDrivingDebugBrowser,
    scrubbing: visuals.scrubbing,
    inspectingRun: explicit,
    blockRunInDebugSession,
    systemFocused: searchParams.has(SYSTEM_RUN_FOCUS_PARAM),
    runInDebugSession,
    running: visuals.running,
    hasRecording: visuals.recordingUrls.length > 0,
    recordingAvailable,
    // Until it is known whether screenshots exist, route as if they do, so the
    // view doesn't flip away and back; the pill itself waits for the answer.
    screenshotsAvailable: screenshotsAvailable || visuals.screenshotsPending,
    hasDebugSession: debugBrowserSessionId != null,
    failed: visuals.failed,
  });

  const liveSurface = resolveLiveSurface({
    recording: userDrivingDebugBrowser,
    running: visuals.running,
    runInDebugSession,
    hasRunId: runId != null,
  });

  return {
    view,
    setView,
    visuals,
    runId,
    inspectingRun: explicit,
    debugBrowserSessionId,
    runInDebugSession,
    liveSurface,
    recordingAvailable,
    screenshotsAvailable,
    liveAvailable: visuals.running || debugBrowserSessionId != null,
  };
}
