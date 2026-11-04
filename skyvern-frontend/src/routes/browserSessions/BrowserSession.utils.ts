import { type BrowserSession } from "@/routes/workflows/types/browserSessionTypes";

const tabNames = ["recordings", "downloads", "timeline", "runs"] as const;

type TabName = "stream" | (typeof tabNames)[number];
type TabOption = { label: string; to: TabName };
type LiveViewContext = Pick<
  BrowserSession,
  | "browser_address"
  | "browser_address_redacted"
  | "source"
  | "status"
  | "vnc_streaming_supported"
>;

const tabOptions: TabOption[] = [
  { label: "Stream", to: "stream" },
  { label: "Recordings", to: "recordings" },
  { label: "Downloads", to: "downloads" },
  { label: "Timeline", to: "timeline" },
  { label: "Runs", to: "runs" },
];

function getBrowserSessionTabOptions(canViewLive: boolean): TabOption[] {
  return canViewLive
    ? tabOptions
    : tabOptions.filter(({ to }) => to !== "stream");
}

function canViewBrowserSessionLive(
  session: LiveViewContext | undefined,
): boolean {
  return Boolean(
    session &&
    !session.browser_address_redacted &&
    (session.browser_address ||
      session.vnc_streaming_supported ||
      session.source === "external" ||
      ["created", "retry", "running"].includes(session.status)),
  );
}

function getBrowserSessionTabFromPathname(pathname: string): TabName {
  return tabNames.find((tab) => pathname.endsWith(`/${tab}`)) ?? "stream";
}

type SessionControlsState = {
  showControls: boolean;
  isSavingProfile: boolean;
  showCloseSession: boolean;
};

// The controls outlive the "running" status: saving a profile closes the session,
// so the in-progress indicator has to survive that transition.
function getSessionControlsState({
  browserSessionId,
  status,
  savingProfileSessionId,
}: {
  browserSessionId: string | undefined;
  status: string | undefined;
  savingProfileSessionId: string | undefined;
}): SessionControlsState {
  const isSavingProfile =
    browserSessionId !== undefined &&
    savingProfileSessionId === browserSessionId;
  const isRunning = status === "running";
  const showControls =
    browserSessionId !== undefined && (isRunning || isSavingProfile);

  return {
    showControls,
    isSavingProfile,
    showCloseSession: showControls && isRunning,
  };
}

export {
  canViewBrowserSessionLive,
  getBrowserSessionTabOptions,
  getBrowserSessionTabFromPathname,
  getSessionControlsState,
  type SessionControlsState,
  type TabName,
};
