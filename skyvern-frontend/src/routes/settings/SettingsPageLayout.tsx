import { Outlet } from "react-router-dom";

function SettingsPageLayout() {
  return (
    <div className="container mx-auto">
      <main>
        <Outlet />
      </main>
    </div>
  );
}

export { SettingsPageLayout };
