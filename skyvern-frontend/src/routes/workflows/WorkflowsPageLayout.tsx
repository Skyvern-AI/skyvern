import { cn } from "@/util/utils";
import { Outlet, useMatch } from "react-router-dom";

function WorkflowsPageLayout() {
  const match = useMatch("/workflows/:workflowPermanentId/edit");

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
