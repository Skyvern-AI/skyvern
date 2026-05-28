import { QuestionMarkIcon } from "@radix-ui/react-icons";
import { BrainIcon } from "@/components/icons/BrainIcon";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";
import { ObserverThought } from "../types/workflowRunTypes";

type Props = {
  active: boolean;
  thought: ObserverThought;
  onClick: (thought: ObserverThought) => void;
  cardClassName?: string;
};

function ThoughtCard({ thought, onClick, active, cardClassName }: Props) {
  const body = thought.answer || thought.thought;
  const titleText = body ? "Thought" : "Thinking";
  const startedAt = basicLocalTimeFormat(thought.created_at);
  const startedAtTitle = basicTimeFormat(thought.created_at);

  return (
    <div
      className={cn(
        "group rounded-md bg-slate-elevation4 ring-1 ring-transparent transition-all duration-200",
        active
          ? "ring-1 ring-white/40 hover:ring-white/40"
          : "hover:ring-white/25",
        cardClassName,
      )}
    >
      <button
        type="button"
        onClick={() => onClick(thought)}
        className="flex w-full cursor-pointer flex-col gap-1 rounded-md px-3 py-2 text-left outline-none focus-visible:ring-1 focus-visible:ring-white/40"
      >
        <div className="flex min-h-[24px] items-center gap-2">
          <BrainIcon className="size-4 shrink-0 text-slate-300" />
          <span className="shrink-0 text-xs text-slate-300">{titleText}</span>
          <span
            className="shrink-0 text-[10px] text-slate-500"
            title={startedAtTitle}
          >
            Started {startedAt}
          </span>
          <span className="ml-auto flex shrink-0 items-center gap-1 rounded bg-slate-elevation5 px-1.5 py-0.5 text-[10px] text-slate-400">
            <QuestionMarkIcon className="size-3" />
            Decision
          </span>
        </div>
        {body && (
          <div className="whitespace-pre-wrap break-words text-xs text-slate-200">
            {body}
          </div>
        )}
      </button>
    </div>
  );
}

export { ThoughtCard };
