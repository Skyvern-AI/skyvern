import { useMatch, useSearchParams } from "react-router-dom";

import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { useAgentsPathMatch } from "@/routes/workflows/useAgentsPathMatch";
import { useStudioRunRouteMatch } from "@/routes/workflows/useStudioRunRouteMatch";
import { useNotFoundVisible } from "@/store/NotFoundStore";

type Options = {
  hideBrowserSessions?: boolean;
  revealOnNotFound?: boolean;
};

function useSidebarHidden({
  hideBrowserSessions = false,
  revealOnNotFound = false,
}: Options = {}) {
  const [searchParams] = useSearchParams();
  const embed = searchParams.get("embed");
  const notFoundVisible = useNotFoundVisible();
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

  // Embedded views are chrome-free by contract, so they stay hidden even on a
  // not-found screen.
  if (embed === "true") {
    return true;
  }

  // The route still looks full-bleed, but a not-found screen has nothing to
  // fill the viewport with — and hiding the chrome strands the user with no way
  // to switch to the organization the linked resource belongs to.
  if (revealOnNotFound && notFoundVisible) {
    return false;
  }

  return Boolean(
    (studioEnabled && runStudioMatch) ||
    workflowEditMatch ||
    workflowStudioMatch ||
    workflowBuildMatch ||
    workflowBlockBuildMatch ||
    workflowDebugMatch ||
    workflowBlockDebugMatch ||
    (hideBrowserSessions && (browserSessionMatch || nestedBrowserSessionMatch)),
  );
}

export { useSidebarHidden };
