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
  const completedAt = browserSession.completed_at
    ? Date.parse(normalizeUtcTimestamp(browserSession.completed_at))
    : Number.NaN;
  const completedAgoMs = now - completedAt;
  if (
    !browserSession.recordings?.length &&
    completedAgoMs >= 0 &&
    completedAgoMs < POST_TERMINAL_REFETCH_WINDOW_MS
  ) {
    return POST_TERMINAL_REFETCH_INTERVAL_MS;
  }
  return false;
}

export { areRecordingsIncomplete, getBrowserSessionRefetchIntervalMs };
