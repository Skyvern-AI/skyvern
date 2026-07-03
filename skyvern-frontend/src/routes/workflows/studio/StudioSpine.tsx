import { useState, type KeyboardEvent } from "react";

import { Status } from "@/api/types";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { cn } from "@/util/utils";

import { studioPanelId, studioTabId } from "./constants";
import { STUDIO_PANE_META } from "./paneMeta";
import { STUDIO_PANE_IDS, type StudioPaneId } from "./panes";
import { useStudioPanes } from "./useStudioPanes";
import { useStudioRunSignals } from "./useStudioRunSignals";

// Terminal-only (finalizedRunStatus never returns a live status); mirrors the
// StatusBadge variant buckets so the dot reads the same as the run chip.
function runStatusDotClass(status: Status): string {
  switch (status) {
    case Status.Completed:
      return "bg-badge-success";
    case Status.Terminated:
      return "bg-badge-terminated";
    case Status.Failed:
    case Status.Canceled:
    case Status.TimedOut:
      return "bg-badge-destructive";
    default:
      // Fallback for terminal statuses added after this mapping.
      return "bg-badge-warning";
  }
}

function runStatusLabel(status: Status): string {
  return status === Status.TimedOut ? "timed out" : status;
}

/**
 * The studio's single vertical tab spine: Copilot, Editor, Browser and
 * Timeline are peer tabs, each toggling its pane open or closed on the stage.
 */
export function StudioSpine() {
  const { panes, togglePane } = useStudioPanes();
  const hasUnseenBrowserActivity = useStudioBrowserStore(
    (s) => s.hasUnseenActivity,
  );
  const clearBrowserActivity = useStudioBrowserStore((s) => s.clearActivity);

  const { hasRun, runStatus } = useStudioRunSignals();

  const onToggle = (id: StudioPaneId) => {
    if (id === "browser" && !panes.includes("browser")) {
      clearBrowserActivity();
    }
    togglePane(id);
  };

  // Roving tabindex (WAI-ARIA toolbar): the rail is one tab stop; arrow keys
  // move focus across the enabled tabs, Enter/Space toggles.
  const [focusedId, setFocusedId] = useState<StudioPaneId>(STUDIO_PANE_IDS[0]!);
  const enabledIds = STUDIO_PANE_IDS.filter(
    (id) => !(id === "timeline" && !hasRun),
  );
  const tabStopId = enabledIds.includes(focusedId) ? focusedId : enabledIds[0];
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    const keys = ["ArrowDown", "ArrowUp", "Home", "End"];
    if (!keys.includes(event.key) || enabledIds.length === 0) {
      return;
    }
    event.preventDefault();
    const current = Math.max(
      0,
      enabledIds.indexOf(tabStopId ?? enabledIds[0]!),
    );
    const nextIndex =
      event.key === "ArrowDown"
        ? (current + 1) % enabledIds.length
        : event.key === "ArrowUp"
          ? (current - 1 + enabledIds.length) % enabledIds.length
          : event.key === "Home"
            ? 0
            : enabledIds.length - 1;
    const next = enabledIds[nextIndex]!;
    setFocusedId(next);
    document.getElementById(studioTabId(next))?.focus();
  };

  return (
    <nav
      aria-label="Studio panes"
      className="flex h-full w-16 shrink-0 flex-col items-center gap-1 border-r border-border bg-slate-elevation1 px-1.5 py-3"
      onKeyDown={onKeyDown}
    >
      {STUDIO_PANE_IDS.map((id) => {
        const { label, icon: Icon } = STUDIO_PANE_META[id];
        const open = panes.includes(id);
        const disabled = id === "timeline" && !hasRun;
        const showActivityDot =
          id === "browser" && hasUnseenBrowserActivity && !open;
        const showRunStatusDot = id === "timeline" && Boolean(runStatus);
        const ariaLabel = showActivityDot
          ? "Browser, new activity"
          : id === "timeline" && runStatus
            ? `Timeline, ${runStatusLabel(runStatus)}`
            : undefined;
        return (
          <button
            key={id}
            id={studioTabId(id)}
            type="button"
            aria-expanded={open}
            aria-controls={studioPanelId(id)}
            aria-label={ariaLabel}
            disabled={disabled}
            tabIndex={id === tabStopId ? 0 : -1}
            onFocus={() => setFocusedId(id)}
            title={
              disabled
                ? "Timeline: no runs yet"
                : runStatus && id === "timeline"
                  ? `Timeline: ${runStatusLabel(runStatus)}`
                  : `${open ? "Close" : "Open"} ${label}`
            }
            onClick={() => onToggle(id)}
            className={cn(
              "relative flex w-full flex-col items-center gap-1 rounded-md px-1 py-2 text-[10px] font-medium leading-none transition-colors",
              "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
              open
                ? "bg-studio-accent/15 text-foreground"
                : "text-muted-foreground hover:bg-slate-elevation3 hover:text-foreground",
              disabled &&
                "cursor-default opacity-50 hover:bg-transparent hover:text-muted-foreground",
            )}
          >
            {open ? (
              <span
                aria-hidden
                className="absolute -left-1.5 bottom-2 top-2 w-[3px] rounded-r-full bg-studio-accent"
              />
            ) : null}
            <Icon
              className={cn("size-5", open && "text-studio-accent")}
              aria-hidden
            />
            {label}
            {showActivityDot ? (
              <span
                aria-hidden
                title="New browser activity"
                className="absolute right-1 top-1 flex size-2"
              >
                <span className="absolute inline-flex h-full w-full rounded-full bg-studio-accent opacity-75 motion-safe:animate-ping" />
                <span className="relative inline-flex size-2 rounded-full bg-studio-accent shadow-[0_0_0_3px_rgba(109,108,246,0.20)]" />
              </span>
            ) : showRunStatusDot && runStatus ? (
              <span
                aria-hidden
                className={cn(
                  "absolute right-1 top-1 size-2 rounded-full",
                  runStatusDotClass(runStatus),
                )}
              />
            ) : null}
          </button>
        );
      })}
      <span
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="sr-only"
      >
        {hasUnseenBrowserActivity ? "New browser activity" : ""}
      </span>
    </nav>
  );
}
