import React, { createContext, useMemo } from "react";
import { useMatch } from "react-router-dom";

import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";

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

// Studio offers block runs without the constrained debug-view chrome; the active
// block run is carried in query params (not the path) so the canvas doesn't re-layout.
function useBlockRunsEnabled() {
  const editMatch = useMatch("/workflows/:workflowPermanentId/edit");
  const studioMatch = useMatch("/workflows/:workflowPermanentId/studio");
  const studioEnabled = useWorkflowStudioEnabled();
  return useMemo(
    () => studioEnabled && Boolean(editMatch || studioMatch),
    [studioEnabled, editMatch, studioMatch],
  );
}

export type DebugStoreContextType = {
  isDebugMode: boolean;
  blockRunsEnabled: boolean;
};

// eslint-disable-next-line react-refresh/only-export-components
export const DebugStoreContext = createContext<
  DebugStoreContextType | undefined
>(undefined);

export const DebugStoreProvider: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const isDebugMode = useIsDebugMode();
  const blockRunsEnabled = useBlockRunsEnabled();

  return (
    <DebugStoreContext.Provider value={{ isDebugMode, blockRunsEnabled }}>
      {children}
    </DebugStoreContext.Provider>
  );
};
