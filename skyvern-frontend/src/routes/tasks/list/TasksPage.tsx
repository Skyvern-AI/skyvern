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
      <div className="flex w-fit gap-1 rounded-sm border border-slate-700 p-2">
        <div
          className={cn(
            "cursor-pointer rounded-sm px-3 py-2 hover:bg-slate-700",
            {
              "bg-slate-700": view === "history",
            },
          )}
          onClick={() => setView("history")}
        >
          Run History
        </div>
        <div
          className={cn(
            "cursor-pointer rounded-sm px-3 py-2 hover:bg-slate-700",
            {
              "bg-slate-700": view === "myTasks",
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
