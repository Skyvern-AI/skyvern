import { Outlet } from "react-router-dom";

function TasksPageLayout() {
  return (
    <div className="p-6 flex grow flex-col gap-4">
      <main>
        <Outlet />
      </main>
    </div>
  );
}

export { TasksPageLayout };
