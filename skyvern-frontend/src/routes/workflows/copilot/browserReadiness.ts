type BrowserReadinessInput = {
  requiresLiveBrowser?: boolean;
  isLiveBrowserReady?: boolean;
};

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
