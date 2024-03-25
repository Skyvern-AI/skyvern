import { cn } from "@/util/utils";
import {
  GearIcon,
  ListBulletIcon,
  PlusCircledIcon,
} from "@radix-ui/react-icons";
import { NavLink } from "react-router-dom";

function SideNav() {
  return (
    <nav className="flex flex-col gap-4">
      <NavLink
        to="create"
        className={({ isActive }) => {
          return cn(
            "flex items-center px-6 py-2 hover:bg-primary-foreground rounded-2xl",
            {
              "bg-primary-foreground": isActive,
            },
          );
        }}
      >
        <PlusCircledIcon className="mr-4" />
        <span>New Task</span>
      </NavLink>
      <NavLink
        to="tasks"
        className={({ isActive }) => {
          return cn(
            "flex items-center px-6 py-2 hover:bg-primary-foreground rounded-2xl",
            {
              "bg-primary-foreground": isActive,
            },
          );
        }}
      >
        <ListBulletIcon className="mr-4" />
        <span>Task History</span>
      </NavLink>
      <NavLink
        to="settings"
        className={({ isActive }) => {
          return cn(
            "flex items-center px-6 py-2 hover:bg-primary-foreground rounded-2xl",
            {
              "bg-primary-foreground": isActive,
            },
          );
        }}
      >
        <GearIcon className="mr-4" />
        <span>Settings</span>
      </NavLink>
    </nav>
  );
}

export { SideNav };
