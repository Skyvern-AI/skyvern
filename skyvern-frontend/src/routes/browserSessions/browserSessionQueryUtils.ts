import { type BrowserSession } from "@/routes/workflows/types/browserSessionTypes";

type BrowserSessionReadiness = Partial<
  Pick<BrowserSession, "status" | "browser_address">
>;

function getBrowserSessionRefetchIntervalMs(
  browserSession: BrowserSessionReadiness | undefined,
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
  return false;
}

export { getBrowserSessionRefetchIntervalMs };
