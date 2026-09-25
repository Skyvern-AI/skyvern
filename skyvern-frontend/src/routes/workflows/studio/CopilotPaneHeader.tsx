import { PlusIcon } from "@radix-ui/react-icons";

import { useCopilotHeaderStore } from "@/store/useCopilotHeaderStore";
import { useRecordingStore } from "@/store/useRecordingStore";
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
        content={
          controls.navigationLockedReason ? (
            <span className="block max-w-xs">
              {controls.navigationLockedReason}
            </span>
          ) : (
            "New chat"
          )
        }
        blocked={controls.newChatDisabled}
      >
        <button
          type="button"
          onClick={controls.onNewChat}
          disabled={controls.newChatDisabled}
          aria-label={
            controls.navigationLockedReason
              ? `New chat unavailable: ${controls.navigationLockedReason}`
              : "New chat"
          }
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
  const recording = useRecordingStore(
    (state) => state.isRecording || state.finishRequested || state.isCommitting,
  );
  return (
    <span
      role="img"
      aria-label={
        recording ? "Copilot recording active" : "Copilot session active"
      }
      className={cn(
        "absolute -bottom-0.5 -right-0.5 h-1.5 w-1.5 rounded-full ring-2 ring-slate-elevation1",
        recording ? "bg-red-500" : "bg-success",
      )}
    />
  );
}

export function CopilotRecordingStatus() {
  const recording = useRecordingStore((state) => state.isRecording);
  const finishing = useRecordingStore(
    (state) => state.finishRequested || state.isCommitting,
  );
  if (!recording && !finishing) {
    return null;
  }
  return (
    <span className="flex items-center gap-1 text-[10px] font-medium text-red-600 dark:text-red-400">
      <span
        className={cn(
          "size-1 rounded-full bg-red-500",
          !finishing && "animate-pulse motion-reduce:animate-none",
        )}
        aria-hidden="true"
      />
      {finishing ? "Finishing" : "Recording"}
    </span>
  );
}
