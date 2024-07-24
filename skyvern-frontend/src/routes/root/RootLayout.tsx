import { Link, Outlet } from "react-router-dom";
import { Toaster } from "@/components/ui/toaster";
import { SideNav } from "./SideNav";
import {
  DiscordLogoIcon,
  PinLeftIcon,
  PinRightIcon,
} from "@radix-ui/react-icons";
import { Logo } from "@/components/Logo";
import GitHubButton from "react-github-btn";
import { useState } from "react";
import { cn } from "@/util/utils";
import { Button } from "@/components/ui/button";

function RootLayout() {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  return (
    <>
      <div className="h-full w-full">
        <aside
          className={cn("fixed min-h-screen border-r-2 px-6", {
            "w-64": !sidebarCollapsed,
            "w-28": sidebarCollapsed,
          })}
        >
          <Link to={window.location.origin}>
            <div className="h-24">
              <Logo />
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
        </aside>
        <div className="flex h-24 items-center justify-end gap-4 px-6">
          <Link
            to="https://discord.com/invite/fG2XXEuQX3"
            target="_blank"
            rel="noopener noreferrer"
          >
            <DiscordLogoIcon className="h-7 w-7" />
          </Link>
          <div className="h-7">
            <GitHubButton
              href="https://github.com/skyvern-ai/skyvern"
              data-color-scheme="no-preference: dark; light: dark; dark: dark;"
              data-size="large"
              data-show-count="true"
              aria-label="Star skyvern-ai/skyvern on GitHub"
            >
              Star
            </GitHubButton>
          </div>
        </div>
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
