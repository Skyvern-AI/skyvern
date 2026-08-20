import { useEffect, useState, type KeyboardEvent } from "react";
import { CheckIcon, ChevronDownIcon, CopyIcon } from "@radix-ui/react-icons";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";

import { Status } from "@/api/types";
import {
  iconForStatus,
  variantForStatus,
  type StatusVariant,
} from "@/components/statusVisuals";
import { copyText } from "@/util/copyText";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
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

// Terminal-only by design: finalizedRunStatus (below) only resolves once a run
// is done, so running/queued/created/paused runs render no dot at all — the
// studio surfaces "in progress" elsewhere (the run pane itself), and a dot
// with no fixed color yet would be misleading. Colors key off the same
// variantForStatus buckets StatusBadge uses, so the dot always agrees with
// the run chip.
const dotClassByVariant: Record<StatusVariant, string> = {
  success: "bg-badge-success",
  warning: "bg-badge-warning",
  destructive: "bg-badge-destructive",
  terminated: "bg-badge-terminated",
  secondary: "bg-badge-neutral",
};

function runStatusDotClass(status: Status): string {
  return dotClassByVariant[variantForStatus(status)];
}

function runStatusLabel(status: Status): string {
  return status === Status.TimedOut ? "timed out" : status;
}

// Mirrors the labels' `hidden xl:inline`: below Tailwind's xl the toggles are
// icon-only and the tooltip carries the label; with labels visible, enabled
// toggles have no tooltip (only icon-only controls tooltip) — except the run
// control's status dot, which always tooltips since its color/icon has no
// visible label anywhere in the header.
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
 * run pane uses a split control while a run is inspected: its labeled button
 * toggles that run's pane, and the adjacent chevron opens the Past Runs
 * selector. With no run to inspect, the single Past Runs button opens the
 * selector. Labels collapse to icons below xl so the cluster never crowds the
 * title or the run actions.
 */
export function StudioPaneToggles() {
  const { panes, togglePane, openPane } = useStudioPanes();
  const workflowDeleted = useStudioWorkflowDeletedAt() !== null;
  const hasUnseenBrowserActivity = useStudioBrowserStore(
    (s) => s.hasUnseenActivity,
  );
  const clearBrowserActivity = useStudioBrowserStore((s) => s.clearActivity);

  const { runId, runStatus } = useStudioRunSignals();
  const workflowPermanentId = useWorkflowPermanentId();
  const labelsCollapsed = useLabelsCollapsed();
  const [runsSelectorOpen, setRunsSelectorOpen] = useState(false);
  const [runLinkCopied, setRunLinkCopied] = useState(false);

  // Copies the run's shareable deep link (?wr= names the run) — the thing
  // people actually paste around — not just the raw id.
  const copyRunLink = async () => {
    if (!runId || runLinkCopied) {
      return;
    }
    await copyText(
      `${window.location.origin}/agents/${workflowPermanentId}/studio?wr=${runId}`,
    );
    setRunLinkCopied(true);
    setTimeout(() => setRunLinkCopied(false), 1500);
  };

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

  type StudioControlId = StudioPaneId | "past-runs";
  const controlIds: StudioControlId[] = STUDIO_PANE_IDS.flatMap((id) =>
    id === "overview" && runId ? [id, "past-runs"] : [id],
  );

  // Roving tabindex (WAI-ARIA toolbar): the cluster is one tab stop; arrow
  // keys move focus across the enabled toggles, Enter/Space toggles/opens.
  const [focusedId, setFocusedId] = useState<StudioControlId>(
    STUDIO_PANE_IDS[0]!,
  );
  const enabledIds = controlIds.filter(
    (id) => id === "past-runs" || !paneBlockedByDeletion(id),
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
        const { icon: Icon } = STUDIO_PANE_META[id];
        // The run pane's tab names the inspected run ("View Run: wr_…") so the
        // run id reads from the top bar; railLabel falls back to "Past Runs".
        const label = railLabel(id, runId);
        const open = panes.includes(id);
        const blockedByDeletion = paneBlockedByDeletion(id);
        const isRunControl = id === "overview";
        const disabled = blockedByDeletion;
        const showActivityDot =
          id === "browser" && hasUnseenBrowserActivity && !open;
        const showRunStatusDot = isRunControl && Boolean(runStatus);
        const ariaLabel = showActivityDot
          ? "Browser, new activity"
          : isRunControl && runStatus
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
                  "absolute -right-1 -top-1 flex size-3.5 items-center justify-center rounded-full text-foreground",
                  runStatusDotClass(runStatus),
                )}
              >
                {iconForStatus(runStatus, "size-2.5")}
              </span>
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

        if (isRunControl) {
          const runButton = (
            <button
              id={studioTabId(id)}
              type="button"
              aria-pressed={open}
              {...(runId
                ? {
                    "aria-expanded": open,
                    "aria-controls": studioPanelId(id),
                  }
                : {})}
              aria-label={ariaLabel}
              tabIndex={id === tabStopId ? 0 : -1}
              onFocus={() => setFocusedId(id)}
              onClick={runId ? () => onToggle(id) : undefined}
              className={cn(
                buttonClassName,
                "group",
                runId && "rounded-r-none pr-2",
              )}
            >
              {iconAndDot}
              {runId ? (
                <span
                  role="button"
                  tabIndex={0}
                  aria-label="Copy run link"
                  title="Copy run link"
                  onPointerDown={(event) => event.stopPropagation()}
                  onClick={(event) => {
                    event.stopPropagation();
                    event.preventDefault();
                    void copyRunLink();
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.stopPropagation();
                      event.preventDefault();
                      void copyRunLink();
                    }
                  }}
                  className={cn(
                    "-ml-1.5 hidden w-0 overflow-hidden rounded p-0 opacity-0 transition-all xl:inline-flex",
                    "text-muted-foreground hover:text-foreground",
                    "group-focus-within:ml-0 group-focus-within:w-4 group-focus-within:p-0.5 group-focus-within:opacity-100 group-hover:ml-0 group-hover:w-4 group-hover:p-0.5 group-hover:opacity-100",
                    "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                    runLinkCopied &&
                      "ml-0 w-4 p-0.5 text-foreground opacity-100",
                  )}
                >
                  {runLinkCopied ? (
                    <CheckIcon className="size-3" aria-hidden />
                  ) : (
                    <CopyIcon className="size-3" aria-hidden />
                  )}
                </span>
              ) : null}
            </button>
          );
          const selectorTrigger = runId ? (
            <PopoverTrigger asChild>
              <button
                id={studioTabId("past-runs")}
                type="button"
                aria-label="Past Runs"
                tabIndex={"past-runs" === tabStopId ? 0 : -1}
                onFocus={() => setFocusedId("past-runs")}
                className={cn(
                  "inline-flex h-8 w-7 items-center justify-center rounded-l-none rounded-r-md border-l border-border/60 transition-colors",
                  "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                  open || runsSelectorOpen
                    ? "bg-accent text-foreground"
                    : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
                )}
              >
                <ChevronDownIcon className="size-3.5" aria-hidden />
              </button>
            </PopoverTrigger>
          ) : (
            <PopoverTrigger asChild>{runButton}</PopoverTrigger>
          );
          const tip = runId
            ? `View Run: ${runId}${
                runStatus ? ` · ${runStatusLabel(runStatus)}` : ""
              }`
            : runStatus
              ? `${label} · ${runStatusLabel(runStatus)}`
              : label;
          return (
            <Popover
              key={id}
              open={runsSelectorOpen}
              onOpenChange={setRunsSelectorOpen}
            >
              <span className="inline-flex items-center">
                {runId ? (
                  labelsCollapsed || showRunStatusDot ? (
                    <Tooltip>
                      <TooltipTrigger asChild>{runButton}</TooltipTrigger>
                      <TooltipContent side="bottom">{tip}</TooltipContent>
                    </Tooltip>
                  ) : (
                    runButton
                  )
                ) : null}
                {runId ? (
                  <Tooltip>
                    <TooltipTrigger asChild>{selectorTrigger}</TooltipTrigger>
                    <TooltipContent side="bottom">Past Runs</TooltipContent>
                  </Tooltip>
                ) : labelsCollapsed ? (
                  <Tooltip>
                    <TooltipTrigger asChild>{selectorTrigger}</TooltipTrigger>
                    <TooltipContent side="bottom">{tip}</TooltipContent>
                  </Tooltip>
                ) : (
                  selectorTrigger
                )}
              </span>
              <PopoverContent
                align={runId ? "end" : "start"}
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
