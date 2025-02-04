import { Outlet } from "react-router-dom";

function DiscoverPageLayout() {
  return (
    <div className="container mx-auto">
      <main>
        <Outlet />
      </main>
    </div>
  );
}

export { DiscoverPageLayout };
