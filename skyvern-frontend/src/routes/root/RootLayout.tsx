import { Link, Outlet } from "react-router-dom";
import { Toaster } from "@/components/ui/toaster";
import { SideNav } from "./SideNav";
import { DiscordLogoIcon, GitHubLogoIcon } from "@radix-ui/react-icons";

function RootLayout() {
  return (
    <>
      <div className="w-full h-full px-4 max-w-screen-2xl mx-auto">
        <aside className="fixed w-72 px-6 shrink-0 min-h-screen">
          <Link
            to="https://skyvern.com"
            target="_blank"
            rel="noopener noreferrer"
          >
            <div className="h-24 flex items-center justify-center">
              <img src="/skyvern-logo.png" width={48} height={48} />
              <img src="/skyvern-logo-text.png" height={48} width={192} />
            </div>
          </Link>
          <SideNav />
        </aside>
        <div className="pl-72 h-24 flex justify-end items-center px-6 gap-4">
          <Link
            to="https://discord.com/invite/fG2XXEuQX3"
            target="_blank"
            rel="noopener noreferrer"
          >
            <DiscordLogoIcon className="w-6 h-6 text-gray-400 hover:text-white" />
          </Link>
          <Link
            to="https://github.com/Skyvern-AI/skyvern"
            target="_blank"
            rel="noopener noreferrer"
          >
            <GitHubLogoIcon className="w-6 h-6 text-gray-400 hover:text-white" />
          </Link>
        </div>
        <main className="pl-72">
          <Outlet />
        </main>
        <aside className="w-72 shrink-0"></aside>
      </div>
      <Toaster />
    </>
  );
}

export { RootLayout };
