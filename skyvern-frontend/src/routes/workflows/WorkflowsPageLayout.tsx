import { Outlet } from "react-router-dom";

function WorkflowsPageLayout() {
  return (
    <main className="container mx-auto px-8">
      <Outlet />
    </main>
  );
}

export { WorkflowsPageLayout };
