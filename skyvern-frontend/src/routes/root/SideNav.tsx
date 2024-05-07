import { cn } from "@/util/utils";
import {
  GearIcon,
  ListBulletIcon,
  PlusCircledIcon,
} from "@radix-ui/react-icons";
import { NavLink } from "react-router-dom";

function SideNav() {
  return (
    <nav>
      <NavLink
        to="create"
        className={({ isActive }) => {
          return cn(
            "flex items-center px-6 py-4 hover:bg-primary-foreground rounded-2xl",
            {
              "bg-primary-foreground": isActive,
            },
          );
        }}
      >
        <PlusCircledIcon className="mr-4 w-6 h-6" />
        <span className="text-lg">New Task</span>
      </NavLink>
      <NavLink
        to="tasks"
        className={({ isActive }) => {
          return cn(
            "flex items-center px-6 py-4 hover:bg-primary-foreground rounded-2xl",
            {
              "bg-primary-foreground": isActive,
            },
          );
        }}
      >
        <ListBulletIcon className="mr-4 w-6 h-6" />
        <span className="text-lg">Task History</span>
      </NavLink>
      <NavLink
        to="settings"
        className={({ isActive }) => {
          return cn(
            "flex items-center px-6 py-4 hover:bg-primary-foreground rounded-2xl",
            {
              "bg-primary-foreground": isActive,
            },
          );
        }}
      >
        <GearIcon className="mr-4 w-6 h-6" />
        <span className="text-lg">Settings</span>
      </NavLink>
    </nav>
  );
}

export { SideNav };
