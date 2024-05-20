import { Outlet } from "react-router-dom";

function CreateNewTaskLayout() {
  return (
    <main className="max-w-6xl mx-auto px-8">
      <Outlet />
    </main>
  );
}

export { CreateNewTaskLayout };
