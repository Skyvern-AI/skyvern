import type { StreamDiagnostic } from "@/routes/streaming/StreamDiagnostics";

const TERMINAL_STREAM_STATUSES = new Set([
  "completed",
  "failed",
  "timeout",
  "session_expired",
  "not_found",
]);

const STREAM_RECONNECT_DELAY_MS = 1000;
const STREAM_MAX_RECONNECT_ATTEMPTS = 20;
const STREAM_ABNORMAL_CLOSE_CODE = 1006;
const STREAM_VNC_FALLBACK_CLOSE_CODE = 4001;
const STREAM_VNC_FALLBACK_CLOSE_REASON = "use-vnc-streaming";

function isTerminalStreamStatus(status: string) {
  return TERMINAL_STREAM_STATUSES.has(status);
}

function shouldReconnectStream({
  closeCode,
  closeReason,
  terminalStatusSeen,
  reconnectAttempts,
}: {
  closeCode: number;
  closeReason: string;
  terminalStatusSeen: boolean;
  reconnectAttempts: number;
}) {
  if (terminalStatusSeen) {
    return false;
  }
  if (
    closeCode === STREAM_VNC_FALLBACK_CLOSE_CODE ||
    closeReason === STREAM_VNC_FALLBACK_CLOSE_REASON
  ) {
    return false;
  }
  if (closeCode !== STREAM_ABNORMAL_CLOSE_CODE) {
    return false;
  }
  return reconnectAttempts < STREAM_MAX_RECONNECT_ATTEMPTS;
}

function diagnosticForStatus(status: string): StreamDiagnostic {
  switch (status) {
    case "not_found":
      return {
        title: "We've misplaced this browser session",
        detail: "The backend can't find it for your org.",
        hint: "Refresh the page or spin up a fresh browser session.",
      };
    case "session_expired":
      return {
        title: "This browser session has expired",
        detail:
          "It reached its timeout and was shut down, so there's no browser left to stream.",
        hint: "Create a new browser session to keep working.",
      };
    case "timeout":
      return {
        title: "The browser's gone strangely quiet",
        detail:
          "The stream connected, but no active page showed up to screencast.",
        hint: "Check backend logs for browser launch errors and verify BROWSER_STREAMING_MODE=cdp.",
      };
    case "completed":
      return {
        title: "Browser session complete",
        detail:
          "This session shut down cleanly, so there's no live browser left to stream.",
        hint: "Start a new browser session whenever you're ready to keep working.",
        tone: "success",
      };
    case "failed":
      return {
        title: "This browser session ended early",
        detail:
          "It stopped before finishing, so there's no browser left to stream.",
        hint: "Start a new browser session to try again.",
      };
    default:
      return {
        title: "Waiting for browser frames",
        detail: `The stream is connected and the session status is ${status}.`,
        pending: true,
      };
  }
}

export {
  STREAM_MAX_RECONNECT_ATTEMPTS,
  STREAM_RECONNECT_DELAY_MS,
  diagnosticForStatus,
  isTerminalStreamStatus,
  shouldReconnectStream,
};
