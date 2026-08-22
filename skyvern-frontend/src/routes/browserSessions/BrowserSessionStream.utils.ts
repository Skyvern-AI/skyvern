import type { StreamDiagnostic } from "@/routes/streaming/StreamDiagnostics";
import { isTerminalStreamStatus } from "@/routes/streaming/streamLifecycle";

const BROWSER_SESSION_STREAM_SUBJECT = "browser session";

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
      // A terminal status with no copy of its own is still over, so it must not
      // fall through to the "still working on it" animation.
      if (isTerminalStreamStatus(status)) {
        return {
          title: "This browser session is no longer live",
          detail: `There's no browser left to stream — status: ${status}.`,
          hint: "Start a new browser session to keep working.",
        };
      }
      return {
        title: "Waiting for browser frames",
        detail: `The stream is connected and the session status is ${status}.`,
        pending: true,
      };
  }
}

export { BROWSER_SESSION_STREAM_SUBJECT, diagnosticForStatus };
