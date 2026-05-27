import { Logo } from "@/components/Logo";
import { LogoMinimized } from "@/components/LogoMinimized";
import { SidebarResourceLinks } from "@/components/SidebarResourceLinks";
import { ThemeToggle } from "@/components/ThemeSwitch";
import { useSidebarStore } from "@/store/SidebarStore";
import { Link } from "react-router-dom";
import { SideNav } from "./SideNav";
import { cn } from "@/util/utils";
import { Button } from "@/components/ui/button";
import { ChevronLeftIcon, ChevronRightIcon } from "@radix-ui/react-icons";
import { useSidebarHidden } from "./useSidebarHidden";

type Props = {
  useCollapsedState?: boolean;
};

function SidebarContent({ useCollapsedState }: Props) {
  const { collapsed: collapsedState, setCollapsed } = useSidebarStore();
  const collapsed = useCollapsedState ? collapsedState : false;
  const sidebarHidden = useSidebarHidden({ hideBrowserSessions: true });

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <Link to={window.location.origin}>
        <div
          className={cn(
            "flex h-20 items-center [&>img]:brightness-0 dark:[&>img]:brightness-100",
            {
              "w-16 justify-center px-0 [&>img]:size-9 [&>img]:object-contain":
                collapsed,
              "justify-start px-5": !collapsed,
            },
          )}
        >
          {collapsed ? <LogoMinimized /> : <Logo />}
        </div>
      </Link>
      <div
        className={cn("flex-1 overflow-y-auto", {
          "px-2": collapsed,
          "px-3": !collapsed,
        })}
      >
        <SideNav />
      </div>
      {!sidebarHidden ? <SidebarResourceLinks collapsed={collapsed} /> : null}
      <div
        className={cn(
          "mt-auto flex min-h-14 items-center border-t border-neutral-200 dark:border-white/[0.06]",
          {
            "justify-center": collapsed,
            "justify-end gap-2 px-3": !collapsed,
          },
        )}
      >
        {!collapsed ? <ThemeToggle /> : null}
        <Button
          size="icon"
          variant="ghost"
          className="size-8 text-neutral-500 hover:bg-neutral-200/70 hover:text-neutral-950 dark:hover:bg-white/[0.04] dark:hover:text-neutral-200"
          onClick={() => {
            setCollapsed(!collapsed);
          }}
        >
          {collapsed ? (
            <ChevronRightIcon className="h-6 w-6" />
          ) : (
            <ChevronLeftIcon className="hidden h-6 w-6 lg:block" />
          )}
        </Button>
      </div>
    </div>
  );
}

export { SidebarContent };
