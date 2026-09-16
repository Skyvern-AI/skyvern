import type { BrowserPaneViewIntent } from "@/store/useStudioBrowserStore";

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
  running: boolean;
  hasRecording: boolean;
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
 * inspected run finishes. Without a run named in the URL (edit context) the
 * pane is live from the first frame — a booting debug session shows its
 * connecting state, never a flash of the latest run's replay.
 */
export function resolveBrowserPaneView({
  intent,
  recording,
  scrubbing,
  inspectingRun,
  blockRunInDebugSession,
  systemFocused,
  running,
  hasRecording,
  failed,
}: ResolveBrowserPaneViewArgs): BrowserPaneView {
  // An active recording outranks everything, stored replay intents included:
  // the recorder is driving the live browser and must see it immediately.
  if (recording) {
    return "live";
  }
  if (intent === "live") {
    return "live";
  }
  // A pinned replay intent always presents its surface; when the run has no
  // recording/screenshots (yet), the pane body renders the empty state.
  if (intent === "recording") {
    return "recording";
  }
  if (intent === "screenshots") {
    return "screenshots";
  }
  if (scrubbing) {
    return "screenshots";
  }
  if (systemFocused) {
    return "live";
  }
  if (blockRunInDebugSession) {
    return "live";
  }
  if (running) {
    return "live";
  }
  if (inspectingRun) {
    return hasRecording && !failed ? "recording" : "screenshots";
  }
  // Edit context: the live debug surface immediately (its boot shows the
  // connecting state); the latest run's replays stay one pill-click away.
  return "live";
}
