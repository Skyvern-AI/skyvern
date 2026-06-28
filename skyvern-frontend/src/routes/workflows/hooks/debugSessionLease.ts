import { type BrowserSession } from "@/routes/workflows/types/browserSessionTypes";

const DEBUG_SESSION_EXPIRY_STATUS_REFETCH_MS = 30 * 1000;
// Backend renewal uses DEBUG_SESSION_TIMEOUT_THRESHOLD_MINUTES = 10.
const DEBUG_SESSION_RENEWAL_THRESHOLD_MS = 10 * 60 * 1000;
// Warn 2 min before the backend renewal cutoff so manual renewal can still succeed.
const RENEWAL_WARNING_BUFFER_MS = 2 * 60 * 1000;
const DEBUG_SESSION_EXPIRY_WARNING_THRESHOLD_MS =
  DEBUG_SESSION_RENEWAL_THRESHOLD_MS + RENEWAL_WARNING_BUFFER_MS;

function getBrowserSessionExpiresAtMs(
  browserSession: Pick<BrowserSession, "started_at" | "timeout"> | null,
): number | null {
  if (!browserSession?.started_at || !browserSession.timeout) {
    return null;
  }

  const startedAtMs = new Date(browserSession.started_at).getTime();
  if (Number.isNaN(startedAtMs)) {
    return null;
  }

  return startedAtMs + browserSession.timeout * 60 * 1000;
}

function getBrowserSessionRemainingMs(
  browserSession: Pick<BrowserSession, "started_at" | "timeout"> | null,
  nowMs: number = Date.now(),
): number | null {
  const expiresAtMs = getBrowserSessionExpiresAtMs(browserSession);
  if (expiresAtMs === null) {
    return null;
  }

  return expiresAtMs - nowMs;
}

function formatBrowserSessionRemainingTime(remainingMs: number): string {
  const positiveRemainingMs = Math.max(0, remainingMs);
  if (positiveRemainingMs < 60_000) {
    return "less than 1 minute";
  }

  const remainingMinutes = Math.floor(positiveRemainingMs / 60_000);
  if (remainingMinutes === 1) {
    return "1 minute";
  }
  return `${remainingMinutes} minutes`;
}

export {
  DEBUG_SESSION_EXPIRY_STATUS_REFETCH_MS,
  DEBUG_SESSION_EXPIRY_WARNING_THRESHOLD_MS,
  DEBUG_SESSION_RENEWAL_THRESHOLD_MS,
  formatBrowserSessionRemainingTime,
  getBrowserSessionExpiresAtMs,
  getBrowserSessionRemainingMs,
};
