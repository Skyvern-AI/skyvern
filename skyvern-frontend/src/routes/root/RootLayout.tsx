import { Link, Outlet } from "react-router-dom";
import { Toaster } from "@/components/ui/toaster";
import { SideNav } from "./SideNav";
import { PinLeftIcon, PinRightIcon } from "@radix-ui/react-icons";
import { Logo } from "@/components/Logo";
import { useState } from "react";
import { cn } from "@/util/utils";
import { Button } from "@/components/ui/button";
import { LogoMinimized } from "@/components/LogoMinimized";
import { Header } from "./Header";

function RootLayout() {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  return (
    <>
      <div className="h-full w-full">
        <aside
          className={cn("fixed h-screen min-h-screen border-r-2 px-6", {
            "w-64": !sidebarCollapsed,
            "w-28": sidebarCollapsed,
          })}
        >
          <div className="flex h-full flex-col">
            <Link to={window.location.origin}>
              <div className="flex h-24 items-center">
                {sidebarCollapsed ? <LogoMinimized /> : <Logo />}
              </div>
            </Link>
            <SideNav collapsed={sidebarCollapsed} />
            <div
              className={cn("mt-auto flex min-h-16", {
                "justify-center": sidebarCollapsed,
                "justify-end": !sidebarCollapsed,
              })}
            >
              <Button
                size="icon"
                variant="ghost"
                onClick={() => {
                  setSidebarCollapsed(!sidebarCollapsed);
                }}
              >
                {sidebarCollapsed ? (
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
            "pl-28": sidebarCollapsed,
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
