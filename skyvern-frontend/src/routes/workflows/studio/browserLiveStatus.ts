import type { StreamState } from "@/routes/streaming/streamState";

type BrowserStream = {
  state: StreamState;
};

type DebugBrowserStream = BrowserStream & {
  browserSessionId: string | null;
};

type RunBrowserStream = BrowserStream & {
  workflowRunId: string;
};

/**
 * Whether the Browser pane has confirmed that the browser currently being
 * shown is painting frames. A session or run ID alone is not enough: either
 * can outlive its browser after a run completes.
 */
export function browserIsLive({
  liveSurface,
  debugBrowserSessionId,
  debugStream,
  runId,
  runStream,
}: {
  liveSurface: "debug" | "run";
  debugBrowserSessionId: string | null;
  debugStream: DebugBrowserStream | null;
  runId: string | undefined;
  runStream: RunBrowserStream | null;
}): boolean {
  if (liveSurface === "run") {
    return (
      runStream !== null &&
      runStream.workflowRunId === runId &&
      runStream.state === "live"
    );
  }

  return (
    debugBrowserSessionId != null &&
    debugStream?.browserSessionId === debugBrowserSessionId &&
    debugStream.state === "live"
  );
}
