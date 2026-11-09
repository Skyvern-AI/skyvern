import { TaskHistory } from "./TaskHistory";
import { PromptBox } from "../create/PromptBox";
import { useState } from "react";
import { cn } from "@/util/utils";
import { SavedTasks } from "../create/SavedTasks";

function TasksPage() {
  const [view, setView] = useState<"history" | "myTasks">("history");

  return (
    <div className="space-y-8">
      <PromptBox />
      <div className="flex w-fit gap-1 rounded-sm border border-slate-300 p-2 dark:border-slate-700">
        <div
          className={cn(
            "cursor-pointer rounded-sm px-3 py-2 text-slate-700 hover:bg-slate-100 hover:text-slate-950 dark:text-foreground dark:hover:bg-slate-700 dark:hover:text-foreground",
            {
              "bg-neutral-900 !text-white hover:bg-neutral-900 hover:!text-white dark:bg-slate-700 dark:!text-foreground dark:hover:bg-slate-700":
                view === "history",
            },
          )}
          onClick={() => setView("history")}
        >
          Run History
        </div>
        <div
          className={cn(
            "cursor-pointer rounded-sm px-3 py-2 text-slate-700 hover:bg-slate-100 hover:text-slate-950 dark:text-foreground dark:hover:bg-slate-700 dark:hover:text-foreground",
            {
              "bg-neutral-900 !text-white hover:bg-neutral-900 hover:!text-white dark:bg-slate-700 dark:!text-foreground dark:hover:bg-slate-700":
                view === "myTasks",
            },
          )}
          onClick={() => setView("myTasks")}
        >
          My Tasks
        </div>
      </div>
      {view === "history" && <TaskHistory />}
      {view === "myTasks" && <SavedTasks />}
    </div>
  );
}

export { TasksPage };
