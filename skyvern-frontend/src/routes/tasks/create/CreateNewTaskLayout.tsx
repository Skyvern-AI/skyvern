import { Outlet } from "react-router-dom";

function CreateNewTaskLayout() {
  return (
    <main className="container mx-auto">
      <Outlet />
    </main>
  );
}

export { CreateNewTaskLayout };
