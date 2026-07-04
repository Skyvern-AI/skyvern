import { PlusIcon } from "@radix-ui/react-icons";

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useCopilotHeaderStore } from "@/store/useCopilotHeaderStore";

import { WorkflowCopilotHistory } from "../copilot/WorkflowCopilotHistory";

/**
 * Copilot pane header chrome: the History and New-chat controls the docked
 * chat registers via useCopilotHeaderStore (its own second header row is gone
 * in the studio). Renders nothing while no docked chat is mounted.
 */
export function CopilotPaneControls() {
  const controls = useCopilotHeaderStore((s) => s.controls);
  if (!controls) {
    return null;
  }
  return (
    <>
      {/* Icon-only bordered squares per the studio button grammar; labels
          live in the tooltips and aria-labels. */}
      <WorkflowCopilotHistory
        workflowPermanentId={controls.workflowPermanentId}
        currentChatId={controls.currentChatId}
        onSelect={controls.onSelectChat}
        disabled={controls.disabled}
        compact
      />
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            onClick={controls.onNewChat}
            aria-label="New chat"
            className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-border text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            <PlusIcon className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </TooltipTrigger>
        <TooltipContent side="bottom">New chat</TooltipContent>
      </Tooltip>
    </>
  );
}

/**
 * Presence badge on the Copilot pane-header icon, replacing the old "● Active"
 * text chip (which was an always-on session indicator; the dot keeps that
 * meaning with the state voiced through the aria-label).
 */
export function CopilotActiveDot() {
  return (
    <span
      role="img"
      aria-label="Copilot session active"
      className="absolute -bottom-0.5 -right-0.5 h-1.5 w-1.5 rounded-full bg-success ring-2 ring-slate-elevation1"
    />
  );
}
