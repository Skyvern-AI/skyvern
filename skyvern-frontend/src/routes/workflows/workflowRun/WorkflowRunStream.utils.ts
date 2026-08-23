import type { StreamDiagnostic } from "@/routes/streaming/StreamDiagnostics";
import { isTerminalStreamStatus } from "@/routes/streaming/streamLifecycle";

const WORKFLOW_RUN_STREAM_SUBJECT = "agent run";

// The statuses that mean the run itself is over, as opposed to the stream giving up
// on a run that is still going. Only these are worth refreshing run queries for.
const WORKFLOW_RUN_FINAL_STATUSES = new Set([
  "completed",
  "failed",
  "terminated",
]);

function isWorkflowRunFinalStatus(status: string) {
  return WORKFLOW_RUN_FINAL_STATUSES.has(status);
}

function diagnosticForStatus(status: string): StreamDiagnostic {
  switch (status) {
    case "not_found":
      return {
        title: "We've misplaced this agent run",
        detail: "The backend can't find it for your org.",
      };
    case "timeout":
      return {
        title: "The browser's gone strangely quiet",
        detail: "The run started, but no active page showed up to stream.",
        hint: "Check backend logs for browser launch errors or a streaming-mode mismatch.",
      };
    case "completed":
      return {
        title: "This agent run has wrapped up",
        detail: `It's no longer live — status: ${status}.`,
        tone: "success",
      };
    default:
      // A terminal status with no copy of its own is still over, so it must not
      // fall through to the "still working on it" animation.
      if (isTerminalStreamStatus(status)) {
        return {
          title: "This agent run has wrapped up",
          detail: `It's no longer live — status: ${status}.`,
        };
      }
      return {
        title: "Waiting for browser frames",
        detail: `The stream is connected and the run status is ${status}.`,
        pending: true,
      };
  }
}

export {
  WORKFLOW_RUN_STREAM_SUBJECT,
  diagnosticForStatus,
  isWorkflowRunFinalStatus,
};
