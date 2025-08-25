import { cn } from "@/util/utils";
import { Outlet, useMatch, useSearchParams } from "react-router-dom";

function WorkflowsPageLayout() {
  const [searchParams] = useSearchParams();
  const embed = searchParams.get("embed");
  const match =
    useMatch("/workflows/:workflowPermanentId/edit") ||
    location.pathname.includes("debug") ||
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
