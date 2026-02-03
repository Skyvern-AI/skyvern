import { useSidebarStore } from "@/store/SidebarStore";
import { cn } from "@/util/utils";
import { NavLink, useMatches } from "react-router-dom";
import { Badge } from "./ui/badge";
import { useIsMobile } from "@/hooks/useIsMobile.ts";

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
};

function NavLinkGroup({ title, links }: Props) {
  const isMobile = useIsMobile();
  const { collapsed } = useSidebarStore();
  const matches = useMatches();
  const groupIsActive = matches.some((match) => {
    const inputs = links.map((link) => link.to);
    return inputs.includes(match.pathname);
  });

  return (
    <div
      className={cn("flex flex-col gap-[0.625rem]", {
        "items-center": collapsed,
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
            "text-center": collapsed,
          })}
        >
          {title}
        </div>
      </div>
      <div className="space-y-[1px]]">
        {links.map((link) => {
          return (
            <NavLink
              key={link.to}
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
                    "px-3": collapsed,
                  },
                );
              }}
            >
              <div className="flex justify-between">
                <div className="flex items-center gap-2">
                  {link.icon}
                  {!collapsed && link.label}
                </div>
                {!collapsed && link.disabled && (
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
        })}
      </div>
    </div>
  );
}

export { NavLinkGroup };
