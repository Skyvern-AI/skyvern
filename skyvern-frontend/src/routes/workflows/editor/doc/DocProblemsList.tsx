import { useEffect, useId, useRef, useState } from "react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn } from "@/util/utils";
import { type AppNode } from "../nodes";
import { docProblemsTitle, type DocProblemEntry } from "./docProblems";
import {
  DOC_SHOW_PROBLEMS_EVENT,
  type DocReturnPoint,
  type DocShowProblemsDetail,
  captureReturnPoint,
  resolveReturnPoint,
} from "./docKeys";
import { docProblemStep } from "./docProblemStep";
import { type DocGoToDetails } from "./WorkflowDocActionsContext";

export function DocProblemsList({
  entries,
  byId,
  goTo,
}: {
  entries: DocProblemEntry[];
  byId: ReadonlyMap<string, AppNode>;
  goTo: (id: string, details?: DocGoToDetails) => void;
}) {
  const [open, setOpen] = useState(false);
  const jumped = useRef(false);
  const returnPoint = useRef<DocReturnPoint | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  useEffect(() => {
    let frame = 0;
    const show = (event: Event) => {
      // Where focus was when Run was pressed; the Run dialog has it by now.
      const detail = (event as CustomEvent<DocShowProblemsDetail | undefined>)
        .detail;
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        // Run can ask while the Doc is scrolled down; the popover is anchored to this button.
        trigger.current?.scrollIntoView?.({ block: "nearest" });
        returnPoint.current = detail
          ? detail.returnPoint
          : captureReturnPoint(document.activeElement);
        setOpen(true);
      });
    };
    window.addEventListener(DOC_SHOW_PROBLEMS_EVENT, show);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener(DOC_SHOW_PROBLEMS_EVENT, show);
    };
  }, []);
  const title = docProblemsTitle(entries);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          ref={trigger}
          type="button"
          data-doc-problems
          onClick={() => {
            // Closing from the button leaves focus on it.
            returnPoint.current = null;
          }}
          className="rounded-md border border-border px-2 py-[2px] text-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
        >
          {title}
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        collisionPadding={16}
        aria-labelledby={titleId}
        className="max-h-[min(420px,var(--radix-popover-content-available-height))] w-[min(380px,calc(100vw-32px))] overflow-y-auto p-2"
        onInteractOutside={() => {
          returnPoint.current = null;
        }}
        onCloseAutoFocus={(event) => {
          // Escape returns to where focus was when Run was pressed; an entry's jump keeps the step's focus.
          // A line being typed in blurs when the list takes focus and its edit span is replaced.
          if (jumped.current || returnPoint.current) event.preventDefault();
          if (!jumped.current && returnPoint.current) {
            const target =
              resolveReturnPoint(document, returnPoint.current) ??
              trigger.current;
            target?.focus({ preventScroll: true });
          }
          jumped.current = false;
          returnPoint.current = null;
        }}
      >
        <p id={titleId} className="px-2 pb-1 pt-0.5 text-[13px] font-medium">
          {title}
        </p>
        <ul data-doc-problem-list className="flex flex-col">
          {entries.map((entry) => {
            const node = entry.nodeId ? byId.get(entry.nodeId) : undefined;
            const step = node ? docProblemStep(node) : null;
            const body = (
              <>
                {step && (
                  <span className="block text-[12.5px] text-muted-foreground">
                    {step.type ? `${step.type} · ` : ""}
                    <span className="font-medium text-foreground">
                      {step.name}
                    </span>
                  </span>
                )}
                <span
                  className={cn(
                    "block whitespace-pre-line text-[13px] [overflow-wrap:anywhere]",
                    entry.blocking
                      ? "text-amber-700 dark:text-amber-300"
                      : "text-foreground",
                  )}
                >
                  {!entry.blocking && (
                    <span className="text-muted-foreground">Warning: </span>
                  )}
                  {entry.message}
                </span>
              </>
            );
            return (
              <li key={entry.key}>
                {node ? (
                  <button
                    type="button"
                    className="w-full rounded-md px-2 py-1.5 text-left outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
                    onClick={() => {
                      jumped.current = true;
                      setOpen(false);
                      goTo(node.id, entry.inDetails ? "open" : undefined);
                    }}
                  >
                    {body}
                  </button>
                ) : (
                  <div className="px-2 py-1.5">{body}</div>
                )}
              </li>
            );
          })}
        </ul>
      </PopoverContent>
    </Popover>
  );
}
