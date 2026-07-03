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
  running: boolean;
  debugSessionUp: boolean;
  hasRecording: boolean;
  hasScreenshots: boolean;
  failed: boolean;
};

/**
 * The Browser pane's view machine, ported from RunHero's resolveRunHeroCenterView:
 * live while running, replay (recording/screenshots) on step-select or once the
 * inspected run finishes, and an idle workflow with history but no live session
 * defaults to the last run's replay instead of an empty stream.
 */
export function resolveBrowserPaneView({
  intent,
  recording,
  scrubbing,
  inspectingRun,
  blockRunInDebugSession,
  running,
  debugSessionUp,
  hasRecording,
  hasScreenshots,
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
  // A pinned replay intent can outlive its data (reload, run swap); fall back
  // to the machine until there is something to render.
  if (intent === "recording" && hasRecording) {
    return "recording";
  }
  if (intent === "screenshots" && hasScreenshots) {
    return "screenshots";
  }
  if (scrubbing) {
    return "screenshots";
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
  if (debugSessionUp) {
    return "live";
  }
  if (hasRecording && !failed) {
    return "recording";
  }
  return hasScreenshots ? "screenshots" : "live";
}
