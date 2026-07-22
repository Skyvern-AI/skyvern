import React, { createContext, useMemo } from "react";

import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { useAgentsPathMatch } from "@/routes/workflows/useAgentsPathMatch";
import { useStudioRunRouteMatch } from "@/routes/workflows/useStudioRunRouteMatch";

function useIsDebugMode() {
  const workflowBuildMatch = useAgentsPathMatch("/:workflowPermanentId/build");
  const workflowBlockBuildMatch = useAgentsPathMatch(
    "/:workflowPermanentId/:workflowRunId/:blockLabel/build",
  );
  return useMemo(
    () => Boolean(workflowBuildMatch || workflowBlockBuildMatch),
    [workflowBuildMatch, workflowBlockBuildMatch],
  );
}

// Studio offers block runs without the constrained debug-view chrome; the active
// block run is carried in query params (not the path) so the canvas doesn't re-layout.
function useBlockRunsEnabled() {
  const editMatch = useAgentsPathMatch("/:workflowPermanentId/edit");
  const studioMatch = useAgentsPathMatch("/:workflowPermanentId/studio");
  const runStudioMatch = useStudioRunRouteMatch();
  const studioEnabled = useWorkflowStudioEnabled();
  return useMemo(
    () => studioEnabled && Boolean(editMatch || studioMatch || runStudioMatch),
    [studioEnabled, editMatch, studioMatch, runStudioMatch],
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
