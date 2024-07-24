import { cn } from "@/util/utils";
import {
  GearIcon,
  LightningBoltIcon,
  ListBulletIcon,
  PlusCircledIcon,
} from "@radix-ui/react-icons";
import { NavLink } from "react-router-dom";

type Props = {
  collapsed: boolean;
};

function SideNav({ collapsed }: Props) {
  return (
    <nav className="space-y-2">
      <NavLink
        to="create"
        className={({ isActive }) => {
          return cn(
            "flex h-[3.25rem] items-center gap-4 rounded-2xl px-5 hover:bg-muted",
            {
              "bg-muted": isActive,
            },
          );
        }}
      >
        <PlusCircledIcon className="h-6 w-6" />
        {!collapsed && <span className="text-lg">Create</span>}
      </NavLink>
      <NavLink
        to="tasks"
        className={({ isActive }) => {
          return cn(
            "flex h-[3.25rem] items-center gap-4 rounded-2xl px-5 hover:bg-muted",
            {
              "bg-muted": isActive,
            },
          );
        }}
      >
        <ListBulletIcon className="h-6 w-6" />
        {!collapsed && <span className="text-lg">Tasks</span>}
      </NavLink>
      <NavLink
        to="workflows"
        className={({ isActive }) => {
          return cn(
            "flex h-[3.25rem] items-center gap-4 rounded-2xl px-5 hover:bg-muted",
            {
              "bg-muted": isActive,
            },
          );
        }}
      >
        <LightningBoltIcon className="h-6 w-6" />
        {!collapsed && <span className="text-lg">Workflows</span>}
      </NavLink>
      <NavLink
        to="settings"
        className={({ isActive }) => {
          return cn(
            "flex h-[3.25rem] items-center gap-4 rounded-2xl px-5 hover:bg-muted",
            {
              "bg-muted": isActive,
            },
          );
        }}
      >
        <GearIcon className="h-6 w-6" />
        {!collapsed && <span className="text-lg">Settings</span>}
      </NavLink>
    </nav>
  );
}

export { SideNav };
