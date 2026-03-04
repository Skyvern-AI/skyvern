import { StatusPill } from "@/components/ui/status-pill";
import { QuestionMarkIcon } from "@radix-ui/react-icons";
import { ObserverThought } from "../types/workflowRunTypes";
import { BrainIcon } from "@/components/icons/BrainIcon";
import { RunCard } from "./RunCard";

type Props = {
  active: boolean;
  thought: ObserverThought;
  onClick: (thought: ObserverThought) => void;
};

function ThoughtCard({ thought, onClick, active }: Props) {
  return (
    <RunCard
      active={active}
      onClick={() => onClick(thought)}
      className="space-y-3 p-4"
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
    </RunCard>
  );
}

export { ThoughtCard };
