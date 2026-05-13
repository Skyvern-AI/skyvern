import { create } from "zustand";

type SettingsStore = {
  environment: string;
  /**
   * The user is currently operating or viewing a live, remote browser. NOTE: if
   * the browser is still connecting, or otherwise not ready, then this should
   * be false.
   */
  isUsingABrowser: boolean;
  /**
   * True from the moment the user has initiated a browser session through to
   * when `isUsingABrowser` flips true. Owned by the route, not BrowserStream,
   * so it covers the API-fetch window before BrowserStream mounts (SKY-9777).
   */
  isLoadingABrowser: boolean;
  /**
   * The current browser session ID when a browser is active.
   */
  browserSessionId: string | null;
  organization: string;
  setEnvironment: (environment: string) => void;
  setIsUsingABrowser: (isUsing: boolean) => void;
  setIsLoadingABrowser: (isLoading: boolean) => void;
  setBrowserSessionId: (browserSessionId: string | null) => void;
  setOrganization: (organization: string) => void;
};

const useSettingsStore = create<SettingsStore>((set) => {
  return {
    environment: "local",
    isUsingABrowser: false,
    isLoadingABrowser: false,
    browserSessionId: null,
    organization: "skyvern",
    setEnvironment: (environment: string) => set({ environment }),
    setIsUsingABrowser: (isUsing: boolean) => set({ isUsingABrowser: isUsing }),
    setIsLoadingABrowser: (isLoading: boolean) =>
      set({ isLoadingABrowser: isLoading }),
    setBrowserSessionId: (browserSessionId: string | null) =>
      set({ browserSessionId }),
    setOrganization: (organization: string) => set({ organization }),
  };
});

export { useSettingsStore };
