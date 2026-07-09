import { cn } from "@/util/utils";
import { Outlet, useMatch, useSearchParams } from "react-router-dom";

// Mounted only under /agents (the /workflows alias redirects before this
// renders), so plain canonical-path matches suffice here.
function WorkflowsPageLayout() {
  const [searchParams] = useSearchParams();
  const embed = searchParams.get("embed");
  const workflowEditMatch = useMatch("/agents/:workflowPermanentId/edit");
  const workflowStudioMatch = useMatch("/agents/:workflowPermanentId/studio");
  const workflowBuildMatch = useMatch("/agents/:workflowPermanentId/build");
  const workflowBlockBuildMatch = useMatch(
    "/agents/:workflowPermanentId/:workflowRunId/:blockLabel/build",
  );
  const workflowDebugMatch = useMatch("/agents/:workflowPermanentId/debug");
  const workflowBlockDebugMatch = useMatch(
    "/agents/:workflowPermanentId/:workflowRunId/:blockLabel/debug",
  );
  const match =
    workflowEditMatch ||
    workflowStudioMatch ||
    workflowBuildMatch ||
    workflowBlockBuildMatch ||
    workflowDebugMatch ||
    workflowBlockDebugMatch ||
    embed === "true";
  return (
    <main
      className={cn({
        "container mx-auto": !match,
      })}
    >
      <Outlet />
    </main>
  );
}

export { WorkflowsPageLayout };
