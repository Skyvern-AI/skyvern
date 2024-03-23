import { Settings } from "./settings/Settings";

function Sidebar() {
  return (
    <aside className="w-72 p-6 shrink-0 min-h-screen border-r-2">
      <Settings />
    </aside>
  );
}

export { Sidebar };
