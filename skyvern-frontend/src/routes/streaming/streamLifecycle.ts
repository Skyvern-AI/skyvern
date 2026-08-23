import type { StreamDiagnostic } from "@/routes/streaming/StreamDiagnostics";

/**
 * Shared lifecycle policy for the screencast WebSocket, so the same server status
 * means the same thing to every consumer. `timeout` in particular is emitted by the
 * backend for both browser sessions and workflow runs and used to be terminal for
 * one consumer and invisible to the other (SKY-14617).
 */

// Covers both entity-final statuses (a run/session that is over) and stream-final
// ones (the backend gave up before or during the screencast). Either way the server
// returns right after sending it, so no further frames can arrive on this socket.
const TERMINAL_STREAM_STATUSES = new Set([
  "completed",
  "failed",
  "terminated",
  "canceled",
  "timed_out",
  "timeout",
  "session_expired",
  "not_found",
]);

const STREAM_RECONNECT_DELAY_MS = 1000;
const STREAM_MAX_RECONNECT_DELAY_MS = 15000;
const STREAM_MAX_RECONNECT_ATTEMPTS = 20;
// A frame survives the first few retries so a momentary blip doesn't flash a panel,
// then goes: past this the picture is stale enough that showing it is a lie.
const STREAM_STALE_FRAME_AFTER_ATTEMPTS = 3;
const STREAM_ABNORMAL_CLOSE_CODE = 1006;
const STREAM_VNC_FALLBACK_CLOSE_CODE = 4001;
const STREAM_VNC_FALLBACK_CLOSE_REASON = "use-vnc-streaming";

type StreamStatusMessage = {
  status?: string;
  screenshot?: string;
  format?: string;
  viewport_width?: number;
  viewport_height?: number;
  url?: string;
};

function isTerminalStreamStatus(status: string) {
  return TERMINAL_STREAM_STATUSES.has(status);
}

/**
 * The backend's `_send_status` frame carries a status and nothing else, and it only
 * sends one when it is about to stop streaming. Frames always carry pixels, so a bare
 * status is the stream signing off rather than a mid-stream update.
 */
function isStreamStatusOnlyMessage(message: StreamStatusMessage): boolean {
  return (
    Boolean(message.status) &&
    !message.screenshot &&
    message.url === undefined &&
    message.format === undefined &&
    message.viewport_width === undefined &&
    message.viewport_height === undefined
  );
}

function streamReconnectDelayMs(reconnectAttempts: number): number {
  return Math.min(
    STREAM_RECONNECT_DELAY_MS * 2 ** reconnectAttempts,
    STREAM_MAX_RECONNECT_DELAY_MS,
  );
}

function shouldReconnectStream({
  closeCode,
  closeReason,
  streamFinished,
  reconnectAttempts,
}: {
  closeCode: number;
  closeReason: string;
  // The stream said its last word: a terminal status, or a frame the client could
  // not parse (the backend only sends non-JSON text for credential failures).
  streamFinished: boolean;
  reconnectAttempts: number;
}) {
  if (streamFinished) {
    return false;
  }
  if (
    closeCode === STREAM_VNC_FALLBACK_CLOSE_CODE ||
    closeReason === STREAM_VNC_FALLBACK_CLOSE_REASON
  ) {
    return false;
  }
  // Any other close is worth retrying, including a clean one: the server closes
  // cleanly after the screencast loop ends, and the entity behind it is often still
  // live, so returning here used to leave a frozen frame and no reconnect.
  return reconnectAttempts < STREAM_MAX_RECONNECT_ATTEMPTS;
}

function diagnosticForStreamEnded({
  status,
  subject,
}: {
  status: string;
  subject: string;
}): StreamDiagnostic {
  return {
    title: "This live view has stopped updating",
    detail: `The browser stream ended while the ${subject} is still ${status}, so the last frame you saw is no longer current.`,
  };
}

function diagnosticForReconnectExhausted(subject: string): StreamDiagnostic {
  return {
    title: "Stream connection dropped",
    detail: "The browser stream disconnected and could not reconnect.",
    hint: `Refresh the page to pick the ${subject} back up.`,
  };
}

function reconnectHint(reconnectAttempts: number): string {
  const seconds =
    Math.round(streamReconnectDelayMs(reconnectAttempts - 1) / 100) / 10;
  return `Reconnecting in ${seconds}s (${reconnectAttempts}/${STREAM_MAX_RECONNECT_ATTEMPTS}).`;
}

export {
  STREAM_ABNORMAL_CLOSE_CODE,
  STREAM_MAX_RECONNECT_ATTEMPTS,
  STREAM_MAX_RECONNECT_DELAY_MS,
  STREAM_RECONNECT_DELAY_MS,
  STREAM_STALE_FRAME_AFTER_ATTEMPTS,
  STREAM_VNC_FALLBACK_CLOSE_CODE,
  STREAM_VNC_FALLBACK_CLOSE_REASON,
  diagnosticForReconnectExhausted,
  diagnosticForStreamEnded,
  isStreamStatusOnlyMessage,
  isTerminalStreamStatus,
  reconnectHint,
  shouldReconnectStream,
  streamReconnectDelayMs,
};
