import { useSidebarStore } from "@/store/SidebarStore";
import { cn } from "@/util/utils";
import { SidebarContent } from "./SidebarContent";

function Sidebar() {
  const collapsed = useSidebarStore((state) => state.collapsed);

  return (
    <aside
      className={cn(
        "fixed hidden h-screen min-h-screen border-r-2 px-6 lg:block",
        {
          "w-64": !collapsed,
          "w-28": collapsed,
        },
      )}
    >
      <SidebarContent useCollapsedState />
    </aside>
  );
}

export { Sidebar };
