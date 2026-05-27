import React, { createContext, useMemo } from "react";
import { useMatch } from "react-router-dom";

function useIsDebugMode() {
  const workflowBuildMatch = useMatch("/workflows/:workflowPermanentId/build");
  const workflowBlockBuildMatch = useMatch(
    "/workflows/:workflowPermanentId/:workflowRunId/:blockLabel/build",
  );
  return useMemo(
    () => Boolean(workflowBuildMatch || workflowBlockBuildMatch),
    [workflowBuildMatch, workflowBlockBuildMatch],
  );
}

export type DebugStoreContextType = {
  isDebugMode: boolean;
};

// eslint-disable-next-line react-refresh/only-export-components
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
