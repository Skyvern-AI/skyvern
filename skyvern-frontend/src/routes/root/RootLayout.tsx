import { Link, Outlet } from "react-router-dom";
import { Toaster } from "@/components/ui/toaster";
import { SideNav } from "./SideNav";
import { PinLeftIcon, PinRightIcon } from "@radix-ui/react-icons";
import { Logo } from "@/components/Logo";
import { cn } from "@/util/utils";
import { Button } from "@/components/ui/button";
import { LogoMinimized } from "@/components/LogoMinimized";
import { Header } from "./Header";
import { useSidebarStore } from "@/store/SidebarStore";

function RootLayout() {
  const { collapsed, setCollapsed } = useSidebarStore();

  return (
    <>
      <div className="h-full w-full">
        <aside
          className={cn("fixed h-screen min-h-screen border-r-2 px-6", {
            "w-64": !collapsed,
            "w-28": collapsed,
          })}
        >
          <div className="flex h-full flex-col">
            <Link to={window.location.origin}>
              <div className="flex h-24 items-center">
                {collapsed ? <LogoMinimized /> : <Logo />}
              </div>
            </Link>
            <SideNav collapsed={collapsed} />
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
                  <PinLeftIcon className="h-6 w-6" />
                )}
              </Button>
            </div>
          </div>
        </aside>
        <Header />
        <main
          className={cn("pb-4 pl-64", {
            "pl-28": collapsed,
          })}
        >
          <Outlet />
        </main>
      </div>
      <Toaster />
    </>
  );
}

export { RootLayout };
