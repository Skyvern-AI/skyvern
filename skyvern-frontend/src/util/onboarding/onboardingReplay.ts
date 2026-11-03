// A one-time request to replay the first-run studio tour for a user who already
// saw it, kept outside PostHog; cleared when the tour closes.
const ONBOARDING_REPLAY_KEY = "skyvern.onboardingReplay";

function setOnboardingReplay(requested: boolean): void {
  try {
    if (requested) {
      window.localStorage.setItem(ONBOARDING_REPLAY_KEY, "1");
    } else {
      window.localStorage.removeItem(ONBOARDING_REPLAY_KEY);
    }
  } catch {
    // Storage unavailable: the replay simply doesn't happen.
  }
}

function isOnboardingReplayRequested(): boolean {
  try {
    return window.localStorage.getItem(ONBOARDING_REPLAY_KEY) === "1";
  } catch {
    return false;
  }
}

export { isOnboardingReplayRequested, setOnboardingReplay };
