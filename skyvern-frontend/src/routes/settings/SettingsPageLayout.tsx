import { Outlet } from "react-router-dom";

function SettingsPageLayout() {
  return (
    <div className="container mx-auto px-8">
      <main>
        <Outlet />
      </main>
    </div>
  );
}

export { SettingsPageLayout };
