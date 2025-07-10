import React, { createContext, useMemo } from "react";
import { useLocation } from "react-router-dom";
import { lsKeys } from "@/util/env";

function useIsDebugMode() {
  const location = useLocation();
  return useMemo(
    () => location.pathname.includes("debug"),
    [location.pathname],
  );
}

function getCurrentBrowserSessionId() {
  const stored = localStorage.getItem(lsKeys.optimisticBrowserSession);
  let browserSessionId: string | null = null;
  try {
    const parsed = JSON.parse(stored ?? "");
    const { browser_session_id } = parsed;
    browserSessionId = browser_session_id as string;
  } catch {
    // pass
  }

  return browserSessionId;
}

export type DebugStoreContextType = {
  isDebugMode: boolean;
  getCurrentBrowserSessionId: () => string | null;
};

export const DebugStoreContext = createContext<
  DebugStoreContextType | undefined
>(undefined);

export const DebugStoreProvider: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const isDebugMode = useIsDebugMode();

  return (
    <DebugStoreContext.Provider
      value={{ isDebugMode, getCurrentBrowserSessionId }}
    >
      {children}
    </DebugStoreContext.Provider>
  );
};
