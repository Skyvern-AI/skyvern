import { Link, Outlet } from "react-router-dom";
import { Toaster } from "@/components/ui/toaster";
import { SideNav } from "./SideNav";
import { DiscordLogoIcon } from "@radix-ui/react-icons";
import { Logo } from "@/components/Logo";
import { Profile } from "./Profile";
import { useContext } from "react";
import { UserContext } from "@/store/UserContext";
import GitHubButton from "react-github-btn";

function RootLayout() {
  const user = useContext(UserContext);

  return (
    <>
      <div className="w-full h-full px-4">
        <aside className="fixed w-72 px-6 shrink-0 min-h-screen border-r-2">
          <Link to={window.location.origin}>
            <div className="h-24">
              <Logo />
            </div>
          </Link>
          <SideNav />
          {user ? (
            <div className="absolute bottom-2 left-0 w-72 px-6 shrink-0">
              <Profile name={user.name} />
            </div>
          ) : null}
        </aside>
        <div className="pl-72 h-24 flex justify-end items-center px-6 gap-4">
          <Link
            to="https://discord.com/invite/fG2XXEuQX3"
            target="_blank"
            rel="noopener noreferrer"
          >
            <DiscordLogoIcon className="w-7 h-7" />
          </Link>
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
        <main className="pl-72 pb-4">
          <Outlet />
        </main>
      </div>
      <Toaster />
    </>
  );
}

export { RootLayout };
