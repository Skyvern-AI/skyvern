import { useState } from "react";
import { useSidebarStore } from "@/store/SidebarStore";
import { cn } from "@/util/utils";
import { NavLink, useMatches } from "react-router-dom";
import { Badge } from "./ui/badge";
import { useIsMobile } from "@/hooks/useIsMobile.ts";
import { ChevronDownIcon } from "@radix-ui/react-icons";

type Props = {
  title: string;
  links: Array<{
    label: string;
    to: string;
    newTab?: boolean;
    disabled?: boolean;
    beta?: boolean;
    icon?: React.ReactNode;
  }>;
  collapsible?: boolean;
  initialVisibleCount?: number;
};

type LinkItem = Props["links"][number];

function NavLinkItem({
  link,
  isMobile,
  sidebarCollapsed,
  groupIsActive,
  isPartiallyHidden,
}: {
  link: LinkItem;
  isMobile: boolean;
  sidebarCollapsed: boolean;
  groupIsActive: boolean;
  isPartiallyHidden?: boolean;
}) {
  return (
    <NavLink
      to={link.to}
      target={link.newTab ? "_blank" : undefined}
      rel={link.newTab ? "noopener noreferrer" : undefined}
      className={({ isActive }) => {
        return cn(
          "block rounded-lg py-2 pl-3 text-slate-400 hover:bg-muted hover:text-primary",
          { "py-1 pl-0 text-[0.8rem]": isMobile },
          {
            "bg-muted": isActive,
          },
          {
            "text-primary": groupIsActive,
            "px-3": sidebarCollapsed,
          },
        );
      }}
      style={
        isPartiallyHidden
          ? {
              maskImage:
                "linear-gradient(to bottom, black 0%, transparent 100%)",
              WebkitMaskImage:
                "linear-gradient(to bottom, black 0%, transparent 100%)",
              pointerEvents: "none",
            }
          : undefined
      }
      tabIndex={isPartiallyHidden ? -1 : undefined}
    >
      <div className="flex justify-between">
        <div className="flex items-center gap-2">
          {link.icon}
          {!sidebarCollapsed && link.label}
        </div>
        {!sidebarCollapsed && link.disabled && (
          <Badge
            className="rounded-[40px] px-2 py-1"
            style={{
              backgroundColor: groupIsActive ? "#301615" : "#1E1016",
              color: groupIsActive ? "#EA580C" : "#8D3710",
            }}
          >
            {link.beta ? "Beta" : "Training"}
          </Badge>
        )}
      </div>
    </NavLink>
  );
}

function NavLinkGroup({
  title,
  links,
  collapsible = false,
  initialVisibleCount = 3,
}: Props) {
  const isMobile = useIsMobile();
  const { collapsed: sidebarCollapsed } = useSidebarStore();
  const matches = useMatches();
  const [isExpanded, setIsExpanded] = useState(false);

  const groupIsActive = matches.some((match) => {
    const inputs = links.map((link) => link.to);
    return inputs.includes(match.pathname);
  });

  const shouldCollapse = collapsible && links.length > initialVisibleCount;
  const alwaysVisibleLinks = shouldCollapse
    ? links.slice(0, initialVisibleCount)
    : links;
  const collapsibleLinks = shouldCollapse
    ? links.slice(initialVisibleCount)
    : [];
  const peekLink = collapsibleLinks[0];
  const hiddenCount = collapsibleLinks.length;

  return (
    <div
      className={cn("flex flex-col gap-[0.625rem]", {
        "items-center": sidebarCollapsed,
      })}
    >
      <div
        className={cn("py-2 text-slate-400", {
          "text-primary": groupIsActive,
          "mt-2 py-1 text-[0.8rem] font-medium uppercase": isMobile,
        })}
      >
        <div
          className={cn({
            "text-center": sidebarCollapsed,
          })}
        >
          {title}
        </div>
      </div>
      <div className="relative space-y-[1px]">
        {/* Always visible links */}
        {alwaysVisibleLinks.map((link) => (
          <NavLinkItem
            key={link.to}
            link={link}
            isMobile={isMobile}
            sidebarCollapsed={sidebarCollapsed}
            groupIsActive={groupIsActive}
          />
        ))}

        {/* Collapsible section */}
        {shouldCollapse && (
          <>
            {/* Peek item - fades out when collapsed */}
            <div
              className="transition-all duration-300 ease-in-out"
              style={{
                opacity: isExpanded ? 0 : 1,
                height: isExpanded ? 0 : "auto",
                overflow: "hidden",
                pointerEvents: isExpanded ? "none" : "auto",
              }}
            >
              {peekLink && (
                <NavLinkItem
                  link={peekLink}
                  isMobile={isMobile}
                  sidebarCollapsed={sidebarCollapsed}
                  groupIsActive={groupIsActive}
                  isPartiallyHidden={true}
                />
              )}
            </div>

            {/* Expandable content using CSS grid animation */}
            <div
              className="grid transition-[grid-template-rows] duration-300 ease-in-out"
              style={{
                gridTemplateRows: isExpanded ? "1fr" : "0fr",
              }}
            >
              <div className="overflow-hidden">
                {collapsibleLinks.map((link) => (
                  <NavLinkItem
                    key={link.to}
                    link={link}
                    isMobile={isMobile}
                    sidebarCollapsed={sidebarCollapsed}
                    groupIsActive={groupIsActive}
                  />
                ))}
              </div>
            </div>

            {/* Expand/collapse button */}
            <button
              onClick={() => setIsExpanded(!isExpanded)}
              aria-label={
                sidebarCollapsed
                  ? isExpanded
                    ? "Collapse links"
                    : `Expand ${hiddenCount} more links`
                  : undefined
              }
              className={cn(
                "flex w-full items-center gap-2 rounded-lg py-2 pl-3 text-slate-400 hover:bg-muted hover:text-primary",
                { "py-1 pl-0 text-[0.8rem]": isMobile },
                {
                  "justify-center px-3": sidebarCollapsed,
                },
              )}
            >
              <span
                className="inline-flex transition-transform duration-300 ease-in-out"
                style={{
                  transform: isExpanded ? "rotate(180deg)" : "rotate(0deg)",
                }}
              >
                <ChevronDownIcon className="size-6" />
              </span>
              {!sidebarCollapsed && (
                <span>{isExpanded ? "Show less" : `${hiddenCount} more`}</span>
              )}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

export { NavLinkGroup };
