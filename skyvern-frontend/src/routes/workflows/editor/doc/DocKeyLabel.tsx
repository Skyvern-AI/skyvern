import type { ReactElement, ReactNode } from "react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { docShortcut } from "./docKeys";

/** A control's tooltip text with its shortcut after it, dimmed. */
export function DocKeyLabel({
  label,
  keys,
  shift = false,
}: {
  label: string;
  // The key after Cmd/Ctrl, with Shift when `shift` is set.
  keys: string;
  shift?: boolean;
}) {
  return (
    <span className="inline-flex items-center gap-2">
      {label}
      {/* The tooltip is the inverse of the page, so the dimmed key is too: slate-300 on dark, slate-600 on light, both over 4.5:1. */}
      <span className="text-slate-300 dark:text-slate-600">
        {docShortcut(keys, shift).label}
      </span>
    </span>
  );
}

/** A Doc control with its tooltip, which names the control and its shortcut. */
export function DocKeyTooltip({
  content,
  side,
  children,
}: {
  content: ReactNode;
  side?: "top" | "bottom";
  children: ReactElement;
}) {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>{children}</TooltipTrigger>
        <TooltipContent side={side}>{content}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
