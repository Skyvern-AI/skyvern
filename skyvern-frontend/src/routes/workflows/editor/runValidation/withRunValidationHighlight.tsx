import { ExclamationTriangleIcon } from "@radix-ui/react-icons";
import type { NodeProps } from "@xyflow/react";
import type { ComponentType } from "react";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";

import { RUN_BLOCKING_OUTLINE_CLASSES } from "./runValidationClasses";
import { useLocateBlockStore } from "./useLocateBlockStore";
import { useRunValidationStore } from "./useRunValidationStore";

function rolledUpLabel(count: number): string {
  return count === 1
    ? "1 block inside needs a credential before it can run."
    : `${count} blocks inside need a credential before they can run.`;
}

// The wrapper div is always present so toggling the highlight never remounts the node.
function withRunValidationHighlight<P extends NodeProps>(
  Component: ComponentType<P>,
): ComponentType<P> {
  function RunValidationHighlight(props: P) {
    const needsAttention = useRunValidationStore((state) =>
      state.blockingBlockIds.has(props.id),
    );
    // A collapsed loop/conditional hides its offending descendant's own badge,
    // so surface a rolled-up count on the container instead.
    const descendantCount = useRunValidationStore(
      (state) => state.blockingDescendantCountById.get(props.id) ?? 0,
    );
    const pulsing = useLocateBlockStore(
      (state) => state.pulseNodeId === props.id,
    );
    const showRollup = !needsAttention && descendantCount > 0;

    return (
      <div
        data-run-blocking={needsAttention ? "true" : undefined}
        data-run-blocking-rollup={showRollup ? "true" : undefined}
        className={cn(
          "rounded-lg",
          (needsAttention || showRollup || pulsing) && "relative",
          needsAttention && RUN_BLOCKING_OUTLINE_CLASSES,
          pulsing && "animate-run-blocking-locate motion-reduce:animate-none",
        )}
      >
        {needsAttention ? (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span
                  tabIndex={0}
                  className="absolute -right-2.5 -top-2.5 z-20 flex size-6 items-center justify-center rounded-full border border-amber-300 bg-amber-500 text-slate-950 shadow-md"
                  role="img"
                  aria-label="This login block needs a credential before it can run"
                >
                  <ExclamationTriangleIcon className="size-3.5" />
                </span>
              </TooltipTrigger>
              <TooltipContent className="max-w-xs">
                This login block needs a credential selected before it can run.
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        ) : null}
        {showRollup ? (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span
                  tabIndex={0}
                  className="absolute -right-2.5 -top-2.5 z-20 flex h-6 min-w-6 items-center justify-center gap-1 rounded-full border border-amber-400/60 bg-amber-500/20 px-1.5 text-xs font-semibold text-amber-300 shadow-sm backdrop-blur-sm"
                  role="img"
                  aria-label={rolledUpLabel(descendantCount)}
                >
                  <ExclamationTriangleIcon className="size-3" />
                  {descendantCount}
                </span>
              </TooltipTrigger>
              <TooltipContent className="max-w-xs">
                {rolledUpLabel(descendantCount)} Expand to fix, or use “Locate”.
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        ) : null}
        <Component {...props} />
      </div>
    );
  }
  RunValidationHighlight.displayName = `withRunValidationHighlight(${Component.displayName ?? Component.name ?? "Component"})`;
  return RunValidationHighlight;
}

export { withRunValidationHighlight };
