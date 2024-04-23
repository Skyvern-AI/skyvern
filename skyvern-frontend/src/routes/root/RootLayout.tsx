import { Link, Outlet } from "react-router-dom";
import { Toaster } from "@/components/ui/toaster";
import { SideNav } from "./SideNav";
import { DiscordLogoIcon, GitHubLogoIcon } from "@radix-ui/react-icons";
import { Logo } from "@/components/Logo";
import { ThemeToggle } from "@/components/ThemeSwitch";
import { Profile } from "./Profile";
import { useContext } from "react";
import { UserContext } from "@/store/UserContext";

type Props = {
  onLogout?: () => void;
};

function RootLayout({ onLogout }: Props) {
  const user = useContext(UserContext);

  return (
    <>
      <div className="w-full h-full px-4">
        <aside className="fixed w-72 px-6 shrink-0 min-h-screen border-r-2">
          <Link
            to="https://skyvern.com"
            target="_blank"
            rel="noopener noreferrer"
          >
            <div className="h-24">
              <Logo />
            </div>
          </Link>
          <SideNav />
          {user ? (
            <div className="absolute bottom-2 left-0 w-72 px-6 shrink-0">
              <Profile name={user.name} onLogout={onLogout} />
            </div>
          ) : null}
        </aside>
        <div className="pl-72 h-24 flex justify-end items-center px-6 gap-4">
          <Link
            to="https://discord.com/invite/fG2XXEuQX3"
            target="_blank"
            rel="noopener noreferrer"
          >
            <DiscordLogoIcon className="w-6 h-6" />
          </Link>
          <Link
            to="https://github.com/Skyvern-AI/skyvern"
            target="_blank"
            rel="noopener noreferrer"
          >
            <GitHubLogoIcon className="w-6 h-6" />
          </Link>
          <ThemeToggle />
        </div>
        <main className="pl-72">
          <Outlet />
        </main>
      </div>
      <Toaster />
    </>
  );
}

export { RootLayout };
