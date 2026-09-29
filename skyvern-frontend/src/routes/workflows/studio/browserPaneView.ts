import type { BrowserPaneViewIntent } from "@/store/useStudioBrowserStore";

import type { RunVisuals } from "./useRunVisuals";

export type BrowserPaneView = "live" | "recording" | "screenshots";

type ResolveBrowserPaneViewArgs = {
  intent: BrowserPaneViewIntent;
  // A browser recording is in progress — the user is driving the live debug
  // browser, so the pane must surface it over any replay.
  recording: boolean;
  // ?active= pins a specific timeline step.
  scrubbing: boolean;
  // The URL names a run (?wr= / a run path param) — the pane is inspecting it.
  inspectingRun: boolean;
  // A block-scoped run (?bl=) shares the live debug session; the live browser
  // stays the surface even after it finalizes (the block-iterate loop).
  blockRunInDebugSession: boolean;
  // The Copilot introduced the run focus and still owns its automatic view.
  systemFocused: boolean;
  // The inspected run executed in the live debug session, so that browser is
  // still the run's evidence once it finishes.
  runInDebugSession: boolean;
  running: boolean;
  hasRecording: boolean;
  // Whether the header offers the Recording / Screenshots pill for this run.
  recordingAvailable: boolean;
  screenshotsAvailable: boolean;
  // Live without a debug session is an open-ended "warming up" spinner.
  hasDebugSession: boolean;
  failed: boolean;
};

type ResolveLiveSurfaceArgs = {
  // A browser recording pins the live surface to the debug browser (the
  // recorder drives it), even if an inspected run streams elsewhere.
  recording: boolean;
  running: boolean;
  runInDebugSession: boolean;
  hasRunId: boolean;
};

// A pill is offered only once there is something behind it, running or not:
// recordings land at finalize, screenshots as the run takes each action.
export function resolveReplayAvailability(
  visuals: Pick<
    RunVisuals,
    "recordingUrls" | "recordingArchived" | "hasScreenshots"
  >,
): { recordingAvailable: boolean; screenshotsAvailable: boolean } {
  return {
    recordingAvailable:
      visuals.recordingUrls.length > 0 || visuals.recordingArchived,
    screenshotsAvailable: visuals.hasScreenshots,
  };
}

/**
 * What the Live view shows: the shared debug-session singleton, or the
 * inspected run's own per-run stream (running outside the debug session).
 */
export function resolveLiveSurface({
  recording,
  running,
  runInDebugSession,
  hasRunId,
}: ResolveLiveSurfaceArgs): "debug" | "run" {
  return !recording && running && !runInDebugSession && hasRunId
    ? "run"
    : "debug";
}

/**
 * The Browser pane's view machine, ported from RunHero's resolveRunHeroCenterView:
 * live while running, replay (recording/screenshots) on step-select or once the
 * inspected run finishes; system focus holds Live only while running or when
 * the run executed in the debug session. Without a run named in the URL (edit
 * context) the pane is always live: replays only exist for an open run, so a
 * stored pill intent or a latest-run step pin never surfaces one there.
 */
export function resolveBrowserPaneView({
  intent,
  recording,
  scrubbing,
  inspectingRun,
  blockRunInDebugSession,
  systemFocused,
  runInDebugSession,
  running,
  hasRecording,
  recordingAvailable,
  screenshotsAvailable,
  hasDebugSession,
  failed,
}: ResolveBrowserPaneViewArgs): BrowserPaneView {
  // An active recording outranks everything, stored replay intents included:
  // the recorder is driving the live browser and must see it immediately.
  // With no run open there is nothing to replay (the header hides the pills).
  if (recording || !inspectingRun) {
    return "live";
  }
  // A finished run's Live view is the debug browser; without one it would be an
  // endless "warming up", so a stored Live intent falls through to the replays.
  if (intent === "live" && (running || hasDebugSession)) {
    return "live";
  }
  // A pinned replay intent presents its surface only while its pill is offered;
  // a finished run with nothing to replay there falls through to the default.
  if (intent === "recording" && recordingAvailable) {
    return "recording";
  }
  if (intent === "screenshots" && screenshotsAvailable) {
    return "screenshots";
  }
  if (scrubbing && screenshotsAvailable) {
    return "screenshots";
  }
  // System focus keeps Live only while the run is still on that browser; a
  // finished run that minted its own browser replays like any inspected run.
  if (systemFocused && (running || runInDebugSession)) {
    return "live";
  }
  if (blockRunInDebugSession) {
    return "live";
  }
  if (running) {
    return "live";
  }
  if (hasRecording && !failed) {
    return "recording";
  }
  if (screenshotsAvailable) {
    return "screenshots";
  }
  if (recordingAvailable) {
    return "recording";
  }
  // Nothing to replay: the debug browser if one exists, else the empty state.
  return hasDebugSession ? "live" : "screenshots";
}
