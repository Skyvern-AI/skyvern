import { cn } from "@/util/utils";
import { Outlet, useMatch, useSearchParams } from "react-router-dom";

function WorkflowsPageLayout() {
  const [searchParams] = useSearchParams();
  const embed = searchParams.get("embed");
  const workflowEditMatch = useMatch("/workflows/:workflowPermanentId/edit");
  const workflowBuildMatch = useMatch("/workflows/:workflowPermanentId/build");
  const workflowBlockBuildMatch = useMatch(
    "/workflows/:workflowPermanentId/:workflowRunId/:blockLabel/build",
  );
  const workflowDebugMatch = useMatch("/workflows/:workflowPermanentId/debug");
  const workflowBlockDebugMatch = useMatch(
    "/workflows/:workflowPermanentId/:workflowRunId/:blockLabel/debug",
  );
  const match =
    workflowEditMatch ||
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
