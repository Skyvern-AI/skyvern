import { CompassIcon } from "@/components/icons/CompassIcon";
import { NavLinkGroup } from "@/components/NavLinkGroup";
import { useSidebarStore } from "@/store/SidebarStore";
import { cn } from "@/util/utils";
import {
  CounterClockwiseClockIcon,
  GearIcon,
  GlobeIcon,
  LightningBoltIcon,
} from "@radix-ui/react-icons";
import { KeyIcon } from "@/components/icons/KeyIcon.tsx";

function SideNav() {
  const { collapsed } = useSidebarStore();

  return (
    <nav
      className={cn("space-y-5", {
        "items-center": collapsed,
      })}
    >
      <NavLinkGroup
        title="Build"
        links={[
          {
            label: "Discover",
            to: "/discover",
            icon: <CompassIcon className="size-6" />,
          },
          {
            label: "Workflows",
            to: "/workflows",
            icon: <LightningBoltIcon className="size-6" />,
          },
          {
            label: "Runs",
            to: "/runs",
            icon: <CounterClockwiseClockIcon className="size-6" />,
          },
          {
            label: "Browsers",
            to: "/browser-sessions",
            icon: <GlobeIcon className="size-6" />,
          },
        ]}
      />
      <NavLinkGroup
        title={"General"}
        links={[
          {
            label: "Settings",
            to: "/settings",
            icon: <GearIcon className="size-6" />,
          },
          {
            label: "Credentials",
            to: "/credentials",
            icon: <KeyIcon className="size-6" />,
          },
        ]}
      />
    </nav>
  );
}

export { SideNav };
