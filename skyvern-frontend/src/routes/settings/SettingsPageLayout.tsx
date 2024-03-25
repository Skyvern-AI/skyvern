import { Outlet } from "react-router-dom";

function SettingsPageLayout() {
  return (
    <div className="flex flex-col gap-4 px-6">
      <main>
        <Outlet />
      </main>
    </div>
  );
}

export { SettingsPageLayout };
