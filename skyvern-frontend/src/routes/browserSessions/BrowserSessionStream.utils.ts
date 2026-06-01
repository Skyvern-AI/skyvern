const TERMINAL_STREAM_STATUSES = new Set([
  "completed",
  "failed",
  "timeout",
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

export {
  STREAM_MAX_RECONNECT_ATTEMPTS,
  STREAM_RECONNECT_DELAY_MS,
  isTerminalStreamStatus,
  shouldReconnectStream,
};
