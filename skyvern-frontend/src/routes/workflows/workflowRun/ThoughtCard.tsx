import { StatusPill } from "@/components/ui/status-pill";
import { QuestionMarkIcon } from "@radix-ui/react-icons";
import { ObserverThought } from "../types/workflowRunTypes";
import { cn } from "@/util/utils";
import { BrainIcon } from "@/components/icons/BrainIcon";
import { useCallback } from "react";

type Props = {
  active: boolean;
  thought: ObserverThought;
  onClick: (thought: ObserverThought) => void;
};

function ThoughtCard({ thought, onClick, active }: Props) {
  const refCallback = useCallback((element: HTMLDivElement | null) => {
    if (element && active) {
      element.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }
    // this should only run once at mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
      ref={refCallback}
    >
      <div className="flex justify-between">
        <div className="flex gap-3">
          <BrainIcon className="size-6" />
          {(thought.answer || thought.thought) && <span>Thought</span>}
          {!thought.answer && !thought.thought && <span>Thinking</span>}
        </div>
        <StatusPill icon={<QuestionMarkIcon className="size-4" />}>
          Decision
        </StatusPill>
      </div>
      {(thought.answer || thought.thought) && (
        <div className="text-xs text-slate-400">
          {thought.answer || thought.thought}
        </div>
      )}
    </div>
  );
}

export { ThoughtCard };
