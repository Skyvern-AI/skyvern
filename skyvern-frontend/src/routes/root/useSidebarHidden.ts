import { useMatch, useSearchParams } from "react-router-dom";

import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { useAgentsPathMatch } from "@/routes/workflows/useAgentsPathMatch";
import { useStudioRunRouteMatch } from "@/routes/workflows/useStudioRunRouteMatch";

type Options = {
  hideBrowserSessions?: boolean;
};

function useSidebarHidden({ hideBrowserSessions = false }: Options = {}) {
  const [searchParams] = useSearchParams();
  const embed = searchParams.get("embed");
  const studioEnabled = useWorkflowStudioEnabled();
  const runStudioMatch = useStudioRunRouteMatch();
  const workflowEditMatch = useAgentsPathMatch("/:workflowPermanentId/edit");
  const workflowStudioMatch = useAgentsPathMatch(
    "/:workflowPermanentId/studio",
  );
  const workflowBuildMatch = useAgentsPathMatch("/:workflowPermanentId/build");
  const workflowBlockBuildMatch = useAgentsPathMatch(
    "/:workflowPermanentId/:workflowRunId/:blockLabel/build",
  );
  const workflowDebugMatch = useAgentsPathMatch("/:workflowPermanentId/debug");
  const workflowBlockDebugMatch = useAgentsPathMatch(
    "/:workflowPermanentId/:workflowRunId/:blockLabel/debug",
  );
  const browserSessionMatch = useMatch("/browser-session/:browserSessionId");
  const nestedBrowserSessionMatch = useMatch(
    "/browser-session/:browserSessionId/*",
  );

  return Boolean(
    (studioEnabled && runStudioMatch) ||
    workflowEditMatch ||
    workflowStudioMatch ||
    workflowBuildMatch ||
    workflowBlockBuildMatch ||
    workflowDebugMatch ||
    workflowBlockDebugMatch ||
    (hideBrowserSessions &&
      (browserSessionMatch || nestedBrowserSessionMatch)) ||
    embed === "true",
  );
}

export { useSidebarHidden };
