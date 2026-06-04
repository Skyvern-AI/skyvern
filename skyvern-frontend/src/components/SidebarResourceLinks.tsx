import {
  DiscordLogoIcon,
  ExternalLinkIcon,
  GitHubLogoIcon,
  ReaderIcon,
  StarIcon,
} from "@radix-ui/react-icons";

import {
  starCountFormatter,
  useGithubStarCount,
} from "@/hooks/useGithubStarCount";
import { cn } from "@/util/utils";

type Props = {
  collapsed: boolean;
};

const links = [
  {
    label: "API Docs",
    href: "https://www.skyvern.com/docs",
    icon: <ReaderIcon className="size-4" />,
  },
  {
    label: "GitHub",
    href: "https://github.com/skyvern-ai/skyvern",
    icon: <GitHubLogoIcon className="size-4" />,
    showStars: true,
  },
  {
    label: "Discord",
    href: "https://discord.com/invite/fG2XXEuQX3",
    icon: <DiscordLogoIcon className="size-4" />,
  },
  {
    label: "Book a demo",
    href: "https://www.skyvern.com/contact",
    icon: <ExternalLinkIcon className="size-4" />,
    cta: true,
  },
];

function SidebarResourceLinks({ collapsed }: Props) {
  const { data: starCount } = useGithubStarCount();

  return (
    <div
      className={cn(
        "border-t border-neutral-200 py-2 dark:border-white/[0.06]",
        {
          "px-2": collapsed,
          "px-3": !collapsed,
        },
      )}
    >
      <div
        className={cn("flex gap-1", {
          "flex-col items-center": collapsed,
          "flex-col": !collapsed,
        })}
      >
        {links.map((link) => (
          <a
            key={link.label}
            href={link.href}
            target="_blank"
            rel="noopener noreferrer"
            title={collapsed ? link.label : undefined}
            className={cn(
              "group flex h-7 items-center rounded-md text-[13px] font-medium leading-5 text-neutral-500 antialiased transition-colors duration-100 hover:bg-neutral-200/70 hover:text-neutral-950 dark:text-neutral-400 dark:hover:bg-white/[0.04] dark:hover:text-neutral-200",
              {
                "w-7 justify-center": collapsed,
                "w-full gap-2.5 px-2": !collapsed,
                "border border-sky-500/30 bg-sky-500/10 text-sky-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.65)] hover:bg-sky-500/15 hover:text-sky-800 dark:border-sky-400/35 dark:bg-sky-400/10 dark:text-sky-300 dark:shadow-[inset_0_1px_0_rgba(255,255,255,0.08)] dark:hover:bg-sky-400/20 dark:hover:text-sky-200":
                  link.cta,
              },
            )}
          >
            <span
              className={cn(
                "flex size-4 shrink-0 items-center justify-center text-neutral-400 transition-colors duration-100 group-hover:text-neutral-900 dark:text-neutral-500 dark:group-hover:text-neutral-300",
                {
                  "text-sky-700 group-hover:text-sky-800 dark:text-sky-300 dark:group-hover:text-sky-200":
                    link.cta,
                },
              )}
            >
              {link.icon}
            </span>
            {!collapsed && (
              <>
                <span className="min-w-0 flex-1 truncate">{link.label}</span>
                {link.showStars && typeof starCount === "number" ? (
                  <span className="flex shrink-0 items-center gap-1 text-[11px] text-neutral-400 group-hover:text-neutral-700 dark:text-neutral-500 dark:group-hover:text-neutral-400">
                    <StarIcon className="size-3" />
                    {starCountFormatter.format(starCount)}
                  </span>
                ) : null}
              </>
            )}
          </a>
        ))}
      </div>
    </div>
  );
}

export { SidebarResourceLinks };
