import { Logo } from "@/components/Logo";
import { LogoMinimized } from "@/components/LogoMinimized";
import { useSidebarStore } from "@/store/SidebarStore";
import { Link } from "react-router-dom";
import { SideNav } from "./SideNav";
import { cn } from "@/util/utils";
import { Button } from "@/components/ui/button";
import { PinLeftIcon, PinRightIcon } from "@radix-ui/react-icons";

type Props = {
  useCollapsedState?: boolean;
};

function SidebarContent({ useCollapsedState }: Props) {
  const { collapsed: collapsedState, setCollapsed } = useSidebarStore();
  const collapsed = useCollapsedState ? collapsedState : false;

  return (
    <div className="flex h-full flex-col overflow-y-auto px-6">
      <Link to={window.location.origin}>
        <div className="flex h-24 items-center justify-center">
          {collapsed ? <LogoMinimized /> : <Logo />}
        </div>
      </Link>
      <SideNav />
      <div
        className={cn("mt-auto flex min-h-16", {
          "justify-center": collapsed,
          "justify-end": !collapsed,
        })}
      >
        <Button
          size="icon"
          variant="ghost"
          onClick={() => {
            setCollapsed(!collapsed);
          }}
        >
          {collapsed ? (
            <PinRightIcon className="h-6 w-6" />
          ) : (
            <PinLeftIcon className="hidden h-6 w-6 lg:block" />
          )}
        </Button>
      </div>
    </div>
  );
}

export { SidebarContent };
