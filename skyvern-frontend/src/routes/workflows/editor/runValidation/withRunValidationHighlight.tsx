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
import { useRunValidationStore } from "./useRunValidationStore";

// The wrapper div is always present so toggling the highlight never remounts the node.
function withRunValidationHighlight<P extends NodeProps>(
  Component: ComponentType<P>,
): ComponentType<P> {
  function RunValidationHighlight(props: P) {
    const needsAttention = useRunValidationStore((state) =>
      state.blockingBlockIds.has(props.id),
    );

    return (
      <div
        data-run-blocking={needsAttention ? "true" : undefined}
        className={cn(
          "rounded-lg",
          needsAttention && ["relative", RUN_BLOCKING_OUTLINE_CLASSES],
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
        <Component {...props} />
      </div>
    );
  }
  RunValidationHighlight.displayName = `withRunValidationHighlight(${Component.displayName ?? Component.name ?? "Component"})`;
  return RunValidationHighlight;
}

export { withRunValidationHighlight };
