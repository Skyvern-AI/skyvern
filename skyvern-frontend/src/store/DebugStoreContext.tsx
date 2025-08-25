import React, { createContext, useMemo } from "react";
import { useLocation } from "react-router-dom";

function useIsDebugMode() {
  const location = useLocation();
  return useMemo(
    () => location.pathname.includes("debug"),
    [location.pathname],
  );
}

export type DebugStoreContextType = {
  isDebugMode: boolean;
};

export const DebugStoreContext = createContext<
  DebugStoreContextType | undefined
>(undefined);

export const DebugStoreProvider: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const isDebugMode = useIsDebugMode();

  return (
    <DebugStoreContext.Provider value={{ isDebugMode }}>
      {children}
    </DebugStoreContext.Provider>
  );
};
