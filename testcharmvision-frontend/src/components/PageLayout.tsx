import { Outlet } from "react-router-dom";

function PageLayout() {
  return (
    <div className="container mx-auto">
      <main>
        <Outlet />
      </main>
    </div>
  );
}

export { PageLayout };
