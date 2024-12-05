import { RobotIcon } from "@/components/icons/RobotIcon";
import { NavLinkGroup } from "@/components/NavLinkGroup";
import { useSidebarStore } from "@/store/SidebarStore";
import { cn } from "@/util/utils";
import { GearIcon, LightningBoltIcon } from "@radix-ui/react-icons";

function SideNav() {
  const { collapsed } = useSidebarStore();

  return (
    <nav
      className={cn("space-y-5", {
        "items-center": collapsed,
      })}
    >
      <NavLinkGroup
        title={"Build"}
        links={[
          {
            label: "Tasks",
            to: "/tasks",
            icon: <RobotIcon className="size-6" />,
          },
          {
            label: "Workflows",
            to: "/workflows",
            icon: <LightningBoltIcon className="size-6" />,
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
        ]}
      />
    </nav>
  );
}

export { SideNav };
