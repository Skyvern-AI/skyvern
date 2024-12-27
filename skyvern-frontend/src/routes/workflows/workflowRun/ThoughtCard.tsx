import { PersonIcon } from "@radix-ui/react-icons";
import { ObserverThought } from "../types/workflowRunTypes";
import { cn } from "@/util/utils";

type Props = {
  active: boolean;
  thought: ObserverThought;
  onClick: (thought: ObserverThought) => void;
};

function ThoughtCard({ thought, onClick, active }: Props) {
  return (
    <div
      className={cn(
        "space-y-3 rounded-md border bg-slate-elevation3 p-4 hover:border-slate-50",
        {
          "border-slate-50": active,
        },
      )}
      onClick={() => {
        onClick(thought);
      }}
    >
      <div className="flex justify-between">
        <span>Thought</span>
        <div className="flex items-center gap-1 bg-slate-elevation5">
          <PersonIcon className="size-4" />
          <span className="text-xs">Decision</span>
        </div>
      </div>
      <div className="text-xs text-slate-400">
        {thought.answer || thought.thought}
      </div>
    </div>
  );
}

export { ThoughtCard };
