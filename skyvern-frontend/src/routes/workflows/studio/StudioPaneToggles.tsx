import { useEffect, useState, type KeyboardEvent } from "react";

import { Status } from "@/api/types";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { cn } from "@/util/utils";

import { ControlTooltip } from "./ControlTooltip";
import { PastRunsList } from "./PastRunsList";
import { studioPanelId, studioTabId } from "./constants";
import { STUDIO_PANE_META, railLabel } from "./paneMeta";
import {
  DELETED_WORKFLOW_BLOCKED_PANES,
  STUDIO_PANE_IDS,
  type StudioPaneId,
} from "./panes";
import { useStudioPanes } from "./useStudioPanes";
import { useStudioRunSignals } from "./useStudioRunSignals";
import { useStudioWorkflowDeletedAt } from "./StudioShellContext";

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

// Mirrors the labels' `hidden xl:inline`: below Tailwind's xl the toggles are
// icon-only and the tooltip carries the label; with labels visible, enabled
// toggles have no tooltip (only icon-only controls tooltip).
function useLabelsCollapsed(): boolean {
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") {
      return;
    }
    const query = window.matchMedia("(min-width: 1280px)");
    const update = () => setCollapsed(!query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return collapsed;
}

/**
 * The studio's pane toggles, top-center in the top bar. Copilot, Editor and
 * Browser are peer TOGGLES (multi-active — each opens or closes its pane). The
 * run pane's tab is different: it's the "Past Runs" selector — clicking always
 * opens a run-history popover (it never toggles the pane), and picking a run
 * opens/retargets the run pane. Labels collapse to icons below xl so the
 * cluster never crowds the title or the run actions.
 */
export function StudioPaneToggles() {
  const { panes, togglePane, openPane } = useStudioPanes();
  const workflowDeleted = useStudioWorkflowDeletedAt() !== null;
  const hasUnseenBrowserActivity = useStudioBrowserStore(
    (s) => s.hasUnseenActivity,
  );
  const clearBrowserActivity = useStudioBrowserStore((s) => s.clearActivity);

  const { runStatus } = useStudioRunSignals();
  const labelsCollapsed = useLabelsCollapsed();
  const [runsSelectorOpen, setRunsSelectorOpen] = useState(false);

  // Picking a run in the selector opens/retargets the run pane. The row's own
  // handler pushes ?wr= first; openPane then merges against the live URL, so
  // this materializes the run pane (overview) without dropping the new ?wr=.
  const onSelectRun = () => {
    openPane("overview", { learn: true });
    setRunsSelectorOpen(false);
  };

  const onToggle = (id: StudioPaneId) => {
    if (id === "browser" && !panes.includes("browser")) {
      clearBrowserActivity();
    }
    togglePane(id, { learn: true });
  };

  const paneBlockedByDeletion = (id: StudioPaneId) =>
    workflowDeleted && DELETED_WORKFLOW_BLOCKED_PANES.includes(id);

  // Roving tabindex (WAI-ARIA toolbar): the cluster is one tab stop; arrow
  // keys move focus across the enabled toggles, Enter/Space toggles/opens.
  const [focusedId, setFocusedId] = useState<StudioPaneId>(STUDIO_PANE_IDS[0]!);
  const enabledIds = STUDIO_PANE_IDS.filter((id) => !paneBlockedByDeletion(id));
  const tabStopId = enabledIds.includes(focusedId) ? focusedId : enabledIds[0];
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    const keys = ["ArrowRight", "ArrowLeft", "Home", "End"];
    if (!keys.includes(event.key) || enabledIds.length === 0) {
      return;
    }
    event.preventDefault();
    const current = Math.max(
      0,
      enabledIds.indexOf(tabStopId ?? enabledIds[0]!),
    );
    const nextIndex =
      event.key === "ArrowRight"
        ? (current + 1) % enabledIds.length
        : event.key === "ArrowLeft"
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
      className="flex shrink-0 items-center gap-1.5"
      onKeyDown={onKeyDown}
    >
      {STUDIO_PANE_IDS.map((id) => {
        const { icon: Icon } = STUDIO_PANE_META[id];
        // The run pane's tab reads the static "Past Runs" (railLabel); the
        // dynamic "Run: wr_…" label lives in the pane header, not the rail.
        const label = railLabel(id);
        const open = panes.includes(id);
        const blockedByDeletion = paneBlockedByDeletion(id);
        const isRunSelector = id === "overview";
        const disabled = blockedByDeletion;
        const showActivityDot =
          id === "browser" && hasUnseenBrowserActivity && !open;
        const showRunStatusDot = isRunSelector && Boolean(runStatus);
        const ariaLabel = showActivityDot
          ? "Browser, new activity"
          : isRunSelector && runStatus
            ? `${label}, ${runStatusLabel(runStatus)}`
            : label;
        const iconAndDot = (
          <>
            <Icon className="size-3.5" aria-hidden />
            <span className="hidden xl:inline">{label}</span>
            {showActivityDot ? (
              <span
                aria-hidden
                title="New browser activity"
                className="absolute -right-0.5 -top-0.5 flex size-2"
              >
                <span className="absolute inline-flex h-full w-full rounded-full bg-primary opacity-75 motion-safe:animate-ping" />
                <span className="relative inline-flex size-2 rounded-full bg-primary" />
              </span>
            ) : showRunStatusDot && runStatus ? (
              <span
                aria-hidden
                className={cn(
                  "absolute -right-0.5 -top-0.5 size-2 rounded-full",
                  runStatusDotClass(runStatus),
                )}
              />
            ) : null}
          </>
        );
        const buttonClassName = cn(
          "relative inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-xs font-medium transition-colors",
          "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
          open
            ? "bg-accent text-foreground"
            : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
          disabled && "pointer-events-none opacity-50",
        );

        // The run pane's tab is a popover trigger: clicking opens the run
        // selector (Radix manages aria-haspopup/expanded/controls). Its
        // active state still reflects whether the run pane is open.
        if (isRunSelector) {
          const trigger = (
            <PopoverTrigger asChild>
              <button
                id={studioTabId(id)}
                type="button"
                aria-pressed={open}
                aria-label={ariaLabel}
                tabIndex={id === tabStopId ? 0 : -1}
                onFocus={() => setFocusedId(id)}
                className={buttonClassName}
              >
                {iconAndDot}
              </button>
            </PopoverTrigger>
          );
          const tip =
            runStatus && !showActivityDot
              ? `${label} · ${runStatusLabel(runStatus)}`
              : label;
          return (
            <Popover
              key={id}
              open={runsSelectorOpen}
              onOpenChange={setRunsSelectorOpen}
            >
              {labelsCollapsed ? (
                <ControlTooltip content={tip}>{trigger}</ControlTooltip>
              ) : (
                trigger
              )}
              <PopoverContent
                align="start"
                sideOffset={8}
                className="w-[22rem] p-0"
              >
                <PastRunsList open={runsSelectorOpen} onSelect={onSelectRun} />
              </PopoverContent>
            </Popover>
          );
        }

        const tip = blockedByDeletion
          ? "Source agent deleted"
          : `${open ? "Close" : "Open"} ${label}`;
        const button = (
          <button
            key={id}
            id={studioTabId(id)}
            type="button"
            aria-pressed={open}
            aria-expanded={open}
            aria-controls={studioPanelId(id)}
            aria-label={ariaLabel}
            disabled={disabled}
            tabIndex={id === tabStopId ? 0 : -1}
            onFocus={() => setFocusedId(id)}
            onClick={() => onToggle(id)}
            className={buttonClassName}
          >
            {iconAndDot}
          </button>
        );
        // Disabled toggles always voice their reason; enabled ones tooltip
        // only while icon-collapsed.
        if (!disabled && !labelsCollapsed) {
          return button;
        }
        return (
          <ControlTooltip key={id} content={tip} blocked={disabled}>
            {button}
          </ControlTooltip>
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
