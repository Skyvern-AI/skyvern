import { Outlet } from "react-router-dom";

function HistoryPageLayout() {
  return (
    <div className="container mx-auto">
      <main>
        <Outlet />
      </main>
    </div>
  );
}

export { HistoryPageLayout };
