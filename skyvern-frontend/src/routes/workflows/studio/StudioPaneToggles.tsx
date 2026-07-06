import { useEffect, useState, type KeyboardEvent } from "react";

import { Status } from "@/api/types";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { cn } from "@/util/utils";

import { ControlTooltip } from "./ControlTooltip";
import { studioPanelId, studioTabId } from "./constants";
import { STUDIO_PANE_META } from "./paneMeta";
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
 * The studio's pane toggles, top-center in the top bar: Copilot, Editor,
 * Browser and Overview are peer TOGGLES (multi-active, not exclusive tabs) —
 * each opens or closes its pane on the stage. Labels collapse to icons below
 * xl so the cluster never crowds the title or the run actions.
 */
export function StudioPaneToggles() {
  const { panes, togglePane } = useStudioPanes();
  const workflowDeleted = useStudioWorkflowDeletedAt() !== null;
  const hasUnseenBrowserActivity = useStudioBrowserStore(
    (s) => s.hasUnseenActivity,
  );
  const clearBrowserActivity = useStudioBrowserStore((s) => s.clearActivity);

  const { hasRun, runStatus } = useStudioRunSignals();
  const labelsCollapsed = useLabelsCollapsed();

  const onToggle = (id: StudioPaneId) => {
    if (id === "browser" && !panes.includes("browser")) {
      clearBrowserActivity();
    }
    togglePane(id, { learn: true });
  };

  const paneBlockedByDeletion = (id: StudioPaneId) =>
    workflowDeleted && DELETED_WORKFLOW_BLOCKED_PANES.includes(id);

  // Roving tabindex (WAI-ARIA toolbar): the cluster is one tab stop; arrow
  // keys move focus across the enabled toggles, Enter/Space toggles.
  const [focusedId, setFocusedId] = useState<StudioPaneId>(STUDIO_PANE_IDS[0]!);
  const enabledIds = STUDIO_PANE_IDS.filter(
    (id) => !(id === "overview" && !hasRun) && !paneBlockedByDeletion(id),
  );
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
        const { label, icon: Icon } = STUDIO_PANE_META[id];
        const open = panes.includes(id);
        const blockedByDeletion = paneBlockedByDeletion(id);
        const disabled = (id === "overview" && !hasRun) || blockedByDeletion;
        const showActivityDot =
          id === "browser" && hasUnseenBrowserActivity && !open;
        const showRunStatusDot = id === "overview" && Boolean(runStatus);
        const ariaLabel = showActivityDot
          ? "Browser, new activity"
          : id === "overview" && runStatus
            ? `Overview, ${runStatusLabel(runStatus)}`
            : label;
        const tip = blockedByDeletion
          ? "Source agent deleted"
          : disabled
            ? "Overview: no runs yet"
            : runStatus && id === "overview"
              ? `Overview: ${runStatusLabel(runStatus)}`
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
            className={cn(
              "relative inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-xs font-medium transition-colors",
              "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
              open
                ? "bg-accent text-foreground"
                : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
              disabled && "pointer-events-none opacity-50",
            )}
          >
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
