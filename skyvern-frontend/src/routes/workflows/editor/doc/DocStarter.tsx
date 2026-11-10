import { useState } from "react";
import { CheckIcon, Cross2Icon } from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import { cn } from "@/util/utils";
import { TIPS, docTipIndex, type DocStarterProgress } from "./docStarterTips";
import { docStarterDone } from "./useDocStarter";

export const DOC_SAMPLE_NOTE =
  "Type / to add a step. Plain text, like this line, is a note the agent reads while it runs.";

/** Shown in place of the first line while a doc has no steps; it isn't saved. */
export function DocStarterNote() {
  return (
    <p
      data-doc-starter-note
      className="mb-3 text-base leading-[1.55] text-muted-foreground"
    >
      {DOC_SAMPLE_NOTE}
    </p>
  );
}

export function DocTip({
  progress,
  seen,
  onSeen,
}: {
  progress: DocStarterProgress;
  seen: string[];
  onSeen: (tip: string) => void;
}) {
  const index = docTipIndex(progress, seen);
  const tip = TIPS[index];
  if (!tip) return null;
  return (
    <div
      role="note"
      className="relative mt-3.5 w-[min(290px,100%)] rounded-[9px] bg-foreground px-[13px] pb-[9px] pt-[11px] text-[13.5px] leading-[1.45] text-background shadow-md"
    >
      <span
        aria-hidden
        className="absolute -top-1.5 left-5 size-3 rotate-45 bg-foreground"
      />
      <p className="mb-2">{tip.text}</p>
      <div className="flex items-center justify-between gap-2">
        <span className="opacity-65">
          Tip {index + 1} of {TIPS.length}
        </span>
        <Button
          size="sm"
          variant="outline"
          className="h-auto rounded-md border-current bg-transparent px-2.5 py-[3px] text-[12.5px] font-normal text-background hover:bg-transparent hover:text-background hover:opacity-85"
          onClick={() => onSeen(tip.id)}
        >
          Got it
        </Button>
      </div>
    </div>
  );
}

export function DocGettingStarted({
  progress,
  kept = [],
  onHide,
  folded = false,
  compact = false,
  clearOfPanel = false,
}: {
  // The side panel takes up to 22rem or 36% of the pane on its right; stay left of it.
  clearOfPanel?: boolean;
  // A narrow pane shows a small pill that opens the card, so it never covers the step being edited.
  compact?: boolean;
  progress: DocStarterProgress;
  // Inert pill while a toast needs the corner; it is short-lived, so there is nothing to open.
  folded?: boolean;
  // Items ticked earlier in this agent; they stay ticked when the draft no longer satisfies them.
  kept?: string[];
  onHide: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const asPill = folded || (compact && !expanded);
  const doneIds = [...docStarterDone(progress), ...kept];
  const items: Array<[boolean, string, string]> = [
    [doneIds.includes("step"), "Make a step yourself", "Type / on a line"],
    [
      doneIds.includes("reference"),
      "Use an input or earlier result",
      "Type @ in a sentence",
    ],
    [
      doneIds.includes("nest"),
      "Nest a step",
      "Tab on a line under a loop or If",
    ],
    [doneIds.includes("run"), "Run it", "Run, top right"],
  ];
  const done = items.filter(([d]) => d).length;
  const position = cn(
    "absolute bottom-4 z-40",
    clearOfPanel ? "right-[calc(min(22rem,36%)+1.25rem)]" : "right-5",
  );
  if (asPill) {
    return (
      <button
        type="button"
        aria-label={`Getting started, ${done} of ${items.length} done`}
        disabled={folded}
        onClick={() => setExpanded(true)}
        className={cn(
          position,
          "rounded-full border border-border bg-popover px-3 py-1.5 text-[13px] font-medium text-popover-foreground shadow-md outline-none focus-visible:ring-2 focus-visible:ring-ring",
        )}
      >
        Getting started · {done}/{items.length}
      </button>
    );
  }
  return (
    <aside
      aria-label="Getting started"
      className={cn(
        position,
        "w-[250px] max-w-[calc(100%-2.5rem)] rounded-[10px] border border-border bg-popover px-3 py-2.5 text-[13.5px] text-popover-foreground shadow-md",
      )}
    >
      <div className="flex items-center gap-2">
        <b className="flex-1 text-[13.5px] font-bold">Getting started</b>
        <span className="text-xs text-muted-foreground">
          {done} of {items.length}
        </span>
        <button
          type="button"
          aria-label={compact ? "Collapse checklist" : "Hide checklist"}
          onClick={compact ? () => setExpanded(false) : onHide}
          className="grid size-5 place-items-center rounded text-muted-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Cross2Icon className="size-3" />
        </button>
      </div>
      <div className="my-2 h-1 rounded-full bg-muted">
        <div
          className="h-1 rounded-full bg-green-600 transition-[width] dark:bg-green-500"
          style={{ width: `${(done / items.length) * 100}%` }}
        />
      </div>
      <ul className="flex flex-col gap-1.5">
        {items.map(([isDone, label, hint]) => (
          <li key={label} className="flex items-start gap-2">
            <span
              aria-hidden
              className={cn(
                "mt-0.5 grid size-3.5 shrink-0 place-items-center rounded-sm border border-muted-foreground/60",
                isDone && "border-green-600 bg-green-600 text-white",
              )}
            >
              {isDone && <CheckIcon className="size-3" />}
            </span>
            <span
              className={cn(
                "text-[13.5px] leading-tight",
                isDone && "text-muted-foreground line-through",
              )}
            >
              <span className="sr-only">{isDone ? "Done: " : "To do: "}</span>
              {label}
              <small className="block text-[11.5px] text-muted-foreground no-underline">
                {hint}
              </small>
            </span>
          </li>
        ))}
      </ul>
    </aside>
  );
}
