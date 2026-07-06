import { useState } from "react";
import { Cross2Icon } from "@radix-ui/react-icons";

import { cn } from "@/util/utils";
import { usePasteSkillHintStore } from "@/store/usePasteSkillHintStore";

export function PasteRecordedStepsHint({ className }: { className?: string }) {
  const dismissed = usePasteSkillHintStore((s) => s.dismissed);
  const dismiss = usePasteSkillHintStore((s) => s.dismiss);
  const [expanded, setExpanded] = useState(false);

  if (dismissed) {
    return null;
  }

  return (
    <div
      className={cn(
        "shrink-0 rounded-lg border border-border bg-slate-elevation2 px-3 py-2 text-xs text-muted-foreground",
        className,
      )}
    >
      <div className="flex items-start gap-2">
        <span aria-hidden className="leading-5">
          💡
        </span>
        <div className="flex-1">
          <span>
            Already recorded this with another agent? Paste the steps into
            Copilot to build it here.
          </span>{" "}
          <button
            type="button"
            onClick={() => setExpanded((prev) => !prev)}
            className="font-medium text-foreground underline-offset-2 hover:underline"
          >
            {expanded ? "Hide" : "How?"}
          </button>
        </div>
        <button
          type="button"
          aria-label="Dismiss"
          onClick={dismiss}
          className="text-muted-foreground hover:text-foreground"
        >
          <Cross2Icon className="h-3.5 w-3.5" />
        </button>
      </div>
      {expanded ? (
        <ol className="mt-2 list-decimal space-y-1 pl-8">
          <li>
            In your agent (e.g. the Claude Chrome extension), open your recorded
            workflow and copy its prompt text.
          </li>
          <li>Paste it into the Copilot chat.</li>
          <li>Copilot turns it into a Skyvern agent you can run and edit.</li>
        </ol>
      ) : null}
    </div>
  );
}
