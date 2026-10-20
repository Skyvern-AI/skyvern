import { cloneElement, useId, type ReactElement, type ReactNode } from "react";

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
 *
 * A `reason` replaces `content` in the tooltip and becomes the accessible
 * description of both the wrapper and the control, so the control's name stays
 * its action ("History") and the reason is read even when the tooltip is closed.
 */
export function ControlTooltip({
  content,
  reason,
  blocked = false,
  side = "bottom",
  wrapperClassName,
  children,
}: {
  // Omit for a control whose visible text already names it: it then gets a tooltip only for a reason.
  content?: ReactNode;
  // Why the control is unavailable. Pair it with `blocked` when the control is disabled.
  reason?: string | null;
  // True while the wrapped control is disabled: keeps the tooltip reachable
  // from the keyboard by making the wrapper itself focusable.
  blocked?: boolean;
  side?: "top" | "bottom" | "left" | "right";
  wrapperClassName?: string;
  children: ReactElement;
}) {
  const reasonId = useId();
  if (!reason && content == null) {
    return children;
  }
  // Spread, never `aria-describedby={undefined}`: Radix's Slot lets a child prop override its
  // own, and an explicit undefined would erase the describedby Radix sets while the tip is open.
  const describedBy = reason ? { "aria-describedby": reasonId } : {};
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn(
            "inline-flex shrink-0 rounded-md outline-none focus-visible:ring-1 focus-visible:ring-ring",
            wrapperClassName,
          )}
          {...(blocked ? { tabIndex: 0 } : {})}
          {...describedBy}
        >
          {cloneElement(children, describedBy)}
          {reason ? (
            <span id={reasonId} hidden>
              {reason}
            </span>
          ) : null}
        </span>
      </TooltipTrigger>
      <TooltipContent side={side} className={reason ? "max-w-xs" : undefined}>
        {reason || content}
      </TooltipContent>
    </Tooltip>
  );
}
