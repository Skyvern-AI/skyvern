import { cn } from "@/util/utils";
import {
  GearIcon,
  ListBulletIcon,
  PlusCircledIcon,
} from "@radix-ui/react-icons";
import { Link, useLocation } from "react-router-dom";

function Sidebar() {
  const location = useLocation();
  let page = "tasks";

  if (location.pathname.includes("create")) {
    page = "create";
  } else if (location.pathname.includes("settings")) {
    page = "settings";
  }

  return (
    <aside className="w-72 p-6 shrink-0 min-h-screen border-r-2">
      <nav className="flex flex-col gap-4">
        <Link to="tasks/create">
          <div
            className={cn(
              "flex items-center px-6 py-2 hover:bg-primary-foreground rounded-2xl",
              {
                "bg-primary-foreground": page === "create",
              },
            )}
          >
            <PlusCircledIcon className="mr-4" />
            <span>New Task</span>
          </div>
        </Link>

        <Link to="tasks">
          <div
            className={cn(
              "flex items-center px-6 py-2 hover:bg-primary-foreground rounded-2xl",
              {
                "bg-primary-foreground": page === "tasks",
              },
            )}
          >
            <ListBulletIcon className="mr-4" />
            <span>Task History</span>
          </div>
        </Link>

        <Link to="settings">
          <div
            className={cn(
              "flex items-center px-6 py-2 hover:bg-primary-foreground rounded-2xl",
              {
                "bg-primary-foreground": page === "settings",
              },
            )}
          >
            <GearIcon className="mr-4" />
            <Link to="settings">Settings</Link>
          </div>
        </Link>
      </nav>
    </aside>
  );
}

export { Sidebar };
