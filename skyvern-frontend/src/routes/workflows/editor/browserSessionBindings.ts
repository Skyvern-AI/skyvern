export function resolveWorkspaceBrowserSessionBindings(
  debugBrowserSessionId: string | null,
  activeRunSessionId: string | null,
) {
  return {
    debugBrowserSessionId,
    displayBrowserSessionId: activeRunSessionId ?? debugBrowserSessionId,
  };
}
