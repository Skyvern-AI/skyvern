type BrowserReadinessInput = {
  requiresLiveBrowser?: boolean;
  isLiveBrowserReady?: boolean;
};

type CopilotLiveBrowserReadyInput = {
  displayReady: boolean;
  hasBackendSession: boolean;
  headlessTurnDrainEnabled: boolean;
};

// The headless drain path posts a turn on the backend session id alone,
// decoupling turn-readiness from the VNC/canvas paint signal.
export function resolveCopilotLiveBrowserReady({
  displayReady,
  hasBackendSession,
  headlessTurnDrainEnabled,
}: CopilotLiveBrowserReadyInput): boolean {
  if (!headlessTurnDrainEnabled) {
    return displayReady;
  }
  return displayReady || hasBackendSession;
}

type BrowserPromptQueueInput = BrowserReadinessInput & {
  message?: string;
};

export function shouldWaitForLiveBrowser({
  requiresLiveBrowser = false,
  isLiveBrowserReady = false,
}: BrowserReadinessInput): boolean {
  return requiresLiveBrowser && !isLiveBrowserReady;
}

export function shouldQueuePromptForLiveBrowser({
  requiresLiveBrowser = false,
  isLiveBrowserReady = false,
  message = "",
}: BrowserPromptQueueInput): boolean {
  return (
    shouldWaitForLiveBrowser({ requiresLiveBrowser, isLiveBrowserReady }) &&
    message.trim().length > 0
  );
}
