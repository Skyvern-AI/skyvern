import { DiscordLogoIcon } from "@radix-ui/react-icons";
import GitHubButton from "react-github-btn";
import { Link, useMatch } from "react-router-dom";

function Header() {
  const match = useMatch("/workflows/:workflowPermanentId/edit");

  if (match) {
    return null;
  }

  return (
    <header>
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
    </header>
  );
}

export { Header };
