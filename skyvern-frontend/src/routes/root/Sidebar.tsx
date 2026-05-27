import { useSidebarStore } from "@/store/SidebarStore";
import { cn } from "@/util/utils";
import { SidebarContent } from "./SidebarContent";

function Sidebar() {
  const collapsed = useSidebarStore((state) => state.collapsed);

  return (
    <aside
      className={cn(
        "fixed hidden h-screen min-h-screen border-r border-neutral-200 bg-neutral-50 dark:border-white/[0.06] dark:bg-background lg:block",
        {
          "w-60": !collapsed,
          "w-16": collapsed,
        },
      )}
    >
      <SidebarContent useCollapsedState />
    </aside>
  );
}

export { Sidebar };
