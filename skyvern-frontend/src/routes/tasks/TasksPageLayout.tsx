import { Outlet } from "react-router-dom";

function TasksPageLayout() {
  return (
    <div className="px-6 flex grow flex-col gap-4">
      <main>
        <Outlet />
      </main>
    </div>
  );
}

export { TasksPageLayout };
