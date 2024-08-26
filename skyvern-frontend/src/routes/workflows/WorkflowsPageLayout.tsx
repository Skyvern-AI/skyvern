import { cn } from "@/util/utils";
import { Outlet, useMatch } from "react-router-dom";

function WorkflowsPageLayout() {
  const match = useMatch("/workflows/:workflowPermanentId");

  return (
    <main
      className={cn({
        "container mx-auto px-8": !match,
      })}
    >
      <Outlet />
    </main>
  );
}

export { WorkflowsPageLayout };
