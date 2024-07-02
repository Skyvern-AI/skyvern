import { Outlet } from "react-router-dom";

function CreateNewTaskLayout() {
  return (
    <main className="container mx-auto px-8">
      <Outlet />
    </main>
  );
}

export { CreateNewTaskLayout };
