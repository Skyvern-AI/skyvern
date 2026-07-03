import type { RunCenterView } from "@/store/RunViewStore";

// Rendered center panels are narrower than store intents: "default" is resolved
// away here, while "stream" is renderer-only and never stored as a center tab.
export type RunHeroCenterView =
  | "code"
  | "inputs"
  | "outputs"
  | "stream"
  | "recording"
  | "screenshot";

type ResolveRunHeroCenterViewArgs = {
  centerView: RunCenterView;
  hasScreenshots: boolean;
  hasInputs: boolean;
  hasOutputs: boolean;
  hasRecording: boolean;
  scrubbing: boolean;
  showDebugStream: boolean;
  debugStreamInBrowserPane: boolean;
  recordingOpen: boolean;
  running: boolean;
  failed: boolean;
};

export function resolveRunHeroCenterView({
  centerView,
  hasScreenshots,
  hasInputs,
  hasOutputs,
  hasRecording,
  scrubbing,
  showDebugStream,
  debugStreamInBrowserPane,
  recordingOpen,
  running,
  failed,
}: ResolveRunHeroCenterViewArgs): RunHeroCenterView {
  if (centerView === "screenshots" && hasScreenshots) {
    return "screenshot";
  }
  if (centerView === "code") {
    return "code";
  }
  if (centerView === "inputs" && hasInputs) {
    return "inputs";
  }
  if (centerView === "outputs" && hasOutputs) {
    return "outputs";
  }
  if (centerView === "recording" && hasRecording) {
    return "recording";
  }
  // A recording override can outlive the URLs during a reload; use the normal
  // fallback order until there is a recording to render.
  if (centerView === "screenshot") {
    return "screenshot";
  }
  if (scrubbing) {
    return "screenshot";
  }
  if (showDebugStream) {
    if (recordingOpen && hasRecording) {
      return "recording";
    }
    return debugStreamInBrowserPane ? "screenshot" : "stream";
  }
  if (running) {
    return "stream";
  }
  return hasRecording && !failed ? "recording" : "screenshot";
}
