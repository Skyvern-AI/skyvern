import { useEffect } from "react";

import { useSettingsStore } from "@/store/SettingsStore";

/**
 * Drives `settingsStore.isLoadingABrowser` from the route level so the flag
 * covers the API-fetch window before BrowserStream mounts (SKY-9777).
 */
function useBrowserLoadingFlag(
  shouldFetchDebugSession: boolean,
  readyBrowserSessionId: string | null,
): void {
  const setIsLoadingABrowser = useSettingsStore(
    (state) => state.setIsLoadingABrowser,
  );

  useEffect(() => {
    setIsLoadingABrowser(shouldFetchDebugSession && !readyBrowserSessionId);
  }, [shouldFetchDebugSession, readyBrowserSessionId, setIsLoadingABrowser]);

  useEffect(() => {
    return () => {
      setIsLoadingABrowser(false);
    };
  }, [setIsLoadingABrowser]);
}

export { useBrowserLoadingFlag };
