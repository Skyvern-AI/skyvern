const tabNames = ["recordings", "downloads", "timeline", "runs"] as const;

type TabName = "stream" | (typeof tabNames)[number];

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
  getBrowserSessionTabFromPathname,
  getSessionControlsState,
  type SessionControlsState,
  type TabName,
};
