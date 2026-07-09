import { type ReactElement, type ReactNode } from "react";

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";

/**
 * Radix tooltip for studio chrome controls that can be disabled. A disabled
 * button swallows the trigger's pointer/focus events, so the trigger is a span
 * wrapper (the standard Radix disabled-trigger idiom) — the control must pair
 * this with `disabled:pointer-events-none` so the span receives the hover.
 */
export function ControlTooltip({
  content,
  blocked = false,
  side = "bottom",
  wrapperClassName,
  children,
}: {
  content: ReactNode;
  // True while the wrapped control is disabled: keeps the tooltip reachable
  // from the keyboard by making the wrapper itself focusable.
  blocked?: boolean;
  side?: "top" | "bottom" | "left" | "right";
  wrapperClassName?: string;
  children: ReactElement;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn("inline-flex shrink-0", wrapperClassName)}
          {...(blocked ? { tabIndex: 0 } : {})}
        >
          {children}
        </span>
      </TooltipTrigger>
      <TooltipContent side={side}>{content}</TooltipContent>
    </Tooltip>
  );
}
