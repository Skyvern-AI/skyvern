import {
  DiscordLogoIcon,
  GitHubLogoIcon,
  StarIcon,
} from "@radix-ui/react-icons";
import { Link, useMatch, useSearchParams } from "react-router-dom";
import { NavigationHamburgerMenu } from "./NavigationHamburgerMenu";

function Header() {
  const [searchParams] = useSearchParams();
  const embed = searchParams.get("embed");
  const match =
    useMatch("/workflows/:workflowPermanentId/edit") ||
    location.pathname.includes("build") ||
    location.pathname.includes("debug") ||
    embed === "true";

  if (match) {
    return null;
  }

  return (
    <header>
      <div className="flex h-24 items-center px-6">
        <NavigationHamburgerMenu />
        <div className="ml-auto flex gap-4">
          <Link
            to="https://discord.com/invite/fG2XXEuQX3"
            target="_blank"
            rel="noopener noreferrer"
          >
            <DiscordLogoIcon className="h-7 w-7" />
          </Link>
          <Link
            to="https://github.com/skyvern-ai/skyvern"
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Star skyvern-ai/skyvern on GitHub"
            className="flex items-center gap-1"
          >
            <GitHubLogoIcon className="h-7 w-7" />
            <StarIcon className="h-5 w-5" />
          </Link>
        </div>
      </div>
    </header>
  );
}

export { Header };
