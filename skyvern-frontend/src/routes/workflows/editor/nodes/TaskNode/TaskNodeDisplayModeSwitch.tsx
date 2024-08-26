import { cn } from "@/util/utils";
import { TaskNodeDisplayMode } from "./types";

type Props = {
  value: TaskNodeDisplayMode;
  onChange: (mode: TaskNodeDisplayMode) => void;
};

function TaskNodeDisplayModeSwitch({ value, onChange }: Props) {
  return (
    <div className="flex w-fit gap-1 rounded-sm border border-slate-700 p-2">
      <div
        className={cn("cursor-pointer rounded-sm p-2 hover:bg-slate-700", {
          "bg-slate-700": value === "basic",
        })}
        onClick={() => {
          onChange("basic");
        }}
      >
        Basic
      </div>
      <div
        className={cn("cursor-pointer rounded-sm p-2 hover:bg-slate-700", {
          "bg-slate-700": value === "advanced",
        })}
        onClick={() => {
          onChange("advanced");
        }}
      >
        Advanced
      </div>
    </div>
  );
}

export { TaskNodeDisplayModeSwitch };
