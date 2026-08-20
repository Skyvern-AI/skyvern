import { type BrowserSession } from "@/routes/workflows/types/browserSessionTypes";
import { normalizeUtcTimestamp } from "@/util/timeFormat";

type BrowserSessionReadiness = Partial<
  Pick<BrowserSession, "status" | "browser_address" | "completed_at">
> & { recordings?: BrowserSession["recordings"] | null };

const POST_TERMINAL_REFETCH_INTERVAL_MS = 10_000;
const POST_TERMINAL_REFETCH_WINDOW_MS = 2 * 60 * 1000;

// Playwright finalizes recordings at session close; mid-session files are partial.
function areRecordingsIncomplete(status: string | null | undefined): boolean {
  return status === "running";
}

function getPostTerminalRecordingDeadlineMs(
  browserSession: BrowserSessionReadiness | undefined,
): number | null {
  if (
    !browserSession?.status ||
    browserSession.status === "created" ||
    browserSession.status === "retry" ||
    browserSession.status === "running" ||
    browserSession.recordings?.length
  ) {
    return null;
  }
  const completedAt = browserSession.completed_at
    ? Date.parse(normalizeUtcTimestamp(browserSession.completed_at))
    : Number.NaN;
  return Number.isFinite(completedAt)
    ? completedAt + POST_TERMINAL_REFETCH_WINDOW_MS
    : null;
}

function getBrowserSessionRefetchIntervalMs(
  browserSession: BrowserSessionReadiness | undefined,
  now = Date.now(),
): number | false {
  if (!browserSession?.status) {
    return 1000;
  }
  if (browserSession.status === "running") {
    if (!browserSession.browser_address) {
      return 1000;
    }
    return 5000;
  }
  if (
    browserSession.status === "created" ||
    browserSession.status === "retry"
  ) {
    return 1000;
  }
  const deadline = getPostTerminalRecordingDeadlineMs(browserSession);
  if (
    deadline !== null &&
    now >= deadline - POST_TERMINAL_REFETCH_WINDOW_MS &&
    now < deadline
  ) {
    return POST_TERMINAL_REFETCH_INTERVAL_MS;
  }
  return false;
}

export {
  areRecordingsIncomplete,
  getBrowserSessionRefetchIntervalMs,
  getPostTerminalRecordingDeadlineMs,
};
