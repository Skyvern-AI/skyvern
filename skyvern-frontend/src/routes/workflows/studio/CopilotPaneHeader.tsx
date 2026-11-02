import { PlusIcon } from "@radix-ui/react-icons";

import {
  COPILOT_ATTENTION_DOT,
  useCopilotHeaderStore,
} from "@/store/useCopilotHeaderStore";
import { cn } from "@/util/utils";

import { WorkflowCopilotHistory } from "../copilot/WorkflowCopilotHistory";
import { PANE_HEADER_ICON_BUTTON_CLASS } from "./constants";
import { ControlTooltip } from "./ControlTooltip";

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
        lockedReason={controls.navigationLockedReason ?? undefined}
        compact
      />
      {/* Disabled buttons swallow the trigger's events, so the reason a locked control gives
          has to hang off a focusable wrapper or a keyboard user never reaches it. */}
      <ControlTooltip
        content="New chat"
        reason={controls.navigationLockedReason}
        blocked={controls.newChatDisabled}
      >
        <button
          type="button"
          onClick={controls.onNewChat}
          disabled={controls.newChatDisabled}
          aria-label="New chat"
          className={PANE_HEADER_ICON_BUTTON_CLASS}
        >
          <PlusIcon className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </ControlTooltip>
    </>
  );
}

/**
 * Presence badge on the Copilot pane-header icon, replacing the old "● Active"
 * text chip (which was an always-on session indicator; the dot keeps that
 * meaning with the state voiced through the aria-label).
 */
export function CopilotActiveDot() {
  const attention = useCopilotHeaderStore((state) => state.attention);
  const label = attention
    ? COPILOT_ATTENTION_DOT[attention]
    : "Copilot session active";
  return (
    <span
      role="img"
      aria-label={label}
      title={attention ? label : undefined}
      className={cn(
        "absolute -bottom-0.5 -right-0.5 h-1.5 w-1.5 rounded-full ring-2 ring-slate-elevation1",
        attention ? "bg-amber-500" : "bg-success",
      )}
    />
  );
}
