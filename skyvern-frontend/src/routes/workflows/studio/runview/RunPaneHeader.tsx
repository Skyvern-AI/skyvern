import { useCallback, useState } from "react";
import {
  ActivityLogIcon,
  CodeIcon,
  FileTextIcon,
  ListBulletIcon,
  ReloadIcon,
  Share1Icon,
} from "@radix-ui/react-icons";
import { useParams } from "react-router-dom";

import { ApiWebhookActionsMenu } from "@/components/ApiWebhookActionsMenu";
import { WebhookReplayDialog } from "@/components/WebhookReplayDialog";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useApiCredential } from "@/hooks/useApiCredential";
import { Status } from "@/api/types";
import { statusIsFinalized } from "@/routes/tasks/types";
import { useRunViewStore } from "@/store/RunViewStore";
import { useRunPaneViewStore } from "@/store/useRunPaneViewStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { type ApiCommandOptions } from "@/util/apiCommands";
import { runsApiBaseUrl } from "@/util/env";
import { cn } from "@/util/utils";

import { useIsGeneratingCode } from "../../editor/hooks/useIsGeneratingCode";
import { constructCacheKeyValue } from "../../editor/utils";
import { useWorkflowRunWithWorkflowQuery } from "../../hooks/useWorkflowRunWithWorkflowQuery";
import { PaneHeaderDivider } from "../PaneHeaderDivider";
import { runOutcomeFromStatus } from "../runProjections";
import { studioPanelId } from "../constants";
import { useStudioPaneCompact } from "../StudioShellContext";
import { useStudioInspectedRun } from "../useStudioInspectedRun";
import { useStudioPanes } from "../useStudioPanes";
import { ViewToggle } from "../ViewToggle";

/**
 * Left header cluster of the Overview pane: the Timeline / Inputs / Outputs /
 * Code view toggles, plus the Live handoff chip while the run executes. The
 * selected view lives in useRunPaneViewStore; RunView renders the body.
 */
export function RunPaneViewToggles() {
  const compact = useStudioPaneCompact();
  const { runId } = useStudioInspectedRun();
  const { workflowPermanentId } = useParams();
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery(
    runId ? { workflowRunId: runId } : undefined,
  );
  const view = useRunPaneViewStore((s) => s.view);
  const setView = useRunPaneViewStore((s) => s.setView);
  const jumpToLive = useRunViewStore((s) => s.jumpToLive);
  const setBrowserPaneView = useStudioBrowserStore((s) => s.setView);
  const { openPane } = useStudioPanes();
  const cacheKey = workflowRun?.workflow?.cache_key ?? "";
  const codeGenerating = useIsGeneratingCode({
    cacheKey,
    cacheKeyValue: constructCacheKeyValue({
      codeKey: cacheKey,
      workflow: workflowRun?.workflow,
      workflowRun,
    }),
    // Same guard as the legacy run page: script queries 404 once the source
    // agent is deleted.
    workflowPermanentId: workflowRun?.workflow?.deleted_at
      ? undefined
      : workflowPermanentId,
    workflowRunId: runId,
  });

  const focusBrowserPane = useCallback(() => {
    // An explicit "watch live": unpin back to the live edge and hand the
    // Browser pane a live intent (it may be sitting on a replay view).
    jumpToLive();
    setBrowserPaneView("live");
    openPane("browser");
    // Defer past the pane-open commit so the scroll sees the visible panel.
    requestAnimationFrame(() => {
      const reduceMotion =
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      document.getElementById(studioPanelId("browser"))?.scrollIntoView({
        behavior: reduceMotion ? "auto" : "smooth",
        block: "nearest",
        inline: "nearest",
      });
    });
  }, [jumpToLive, setBrowserPaneView, openPane]);

  if (!workflowRun) {
    return null;
  }
  const outcome = runOutcomeFromStatus(workflowRun.status);
  const provisioning =
    workflowRun.status === Status.Created ||
    workflowRun.status === Status.Queued;
  const showLive = outcome === "running" && !provisioning;

  return (
    <>
      <PaneHeaderDivider />
      <div
        role="group"
        aria-label="Run view"
        className="flex min-w-0 shrink items-center gap-1 overflow-hidden"
      >
        <ViewToggle
          active={view === "timeline"}
          onClick={() => setView("timeline")}
          compact={compact}
          label="Timeline"
          icon={<ActivityLogIcon className="h-3 w-3" />}
        />
        <ViewToggle
          active={view === "inputs"}
          onClick={() => setView("inputs")}
          compact={compact}
          label="Inputs"
          icon={<ListBulletIcon className="h-3 w-3" />}
        />
        <ViewToggle
          active={view === "outputs"}
          onClick={() => setView("outputs")}
          compact={compact}
          label="Outputs"
          icon={<FileTextIcon className="h-3 w-3" />}
        />
        <ViewToggle
          active={view === "code"}
          onClick={() => setView("code")}
          compact={compact}
          label="Code"
          title={
            codeGenerating ? "Generating cached code for this run" : undefined
          }
          icon={
            codeGenerating ? (
              <ReloadIcon
                data-testid="code-generating-spinner"
                className="h-3 w-3 animate-spin"
              />
            ) : (
              <CodeIcon className="h-3 w-3" />
            )
          }
        />
      </div>
      {showLive
        ? (() => {
            const liveChip = (
              <button
                type="button"
                onClick={focusBrowserPane}
                aria-label="Live"
                className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md border border-border px-2.5 text-[11px] font-medium text-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
                {compact ? null : "Live"}
              </button>
            );
            // Labelled → self-describing; icon-collapsed keeps the tooltip.
            return compact ? (
              <Tooltip>
                <TooltipTrigger asChild>{liveChip}</TooltipTrigger>
                <TooltipContent side="bottom">
                  Watch live in the Browser pane
                </TooltipContent>
              </Tooltip>
            ) : (
              liveChip
            );
          })()
        : null}
    </>
  );
}

/**
 * Right header cluster of the Overview pane: the API & Webhooks menu for the
 * inspected run, relocated from the retired run-hero header.
 */
export function RunPaneActions() {
  const compact = useStudioPaneCompact();
  const apiCredential = useApiCredential();
  const { runId } = useStudioInspectedRun();
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery(
    runId ? { workflowRunId: runId } : undefined,
  );
  const [replayOpen, setReplayOpen] = useState(false);
  if (!workflowRun) {
    return null;
  }
  // Legacy parity: the menu re-runs the workflow via API, which is gone with
  // the source agent.
  if (workflowRun.workflow?.deleted_at) {
    return null;
  }
  const finalized = statusIsFinalized(workflowRun);
  return (
    <>
      <ApiWebhookActionsMenu
        triggerTooltip={compact ? "API & Webhooks" : undefined}
        trigger={
          <button
            type="button"
            aria-label="API & Webhooks"
            className={cn(
              "inline-flex h-7 items-center gap-1.5 rounded-md border border-border px-2 text-[11px] font-medium",
              "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
              "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
            )}
          >
            <Share1Icon className="h-3.5 w-3.5" />
            {compact ? null : "API & Webhooks"}
          </button>
        }
        getOptions={() => {
          const headers: Record<string, string> = {
            "Content-Type": "application/json",
            "x-api-key": apiCredential ?? "<your-api-key>",
          };
          const body: Record<string, unknown> = {
            workflow_id: workflowRun.workflow?.workflow_permanent_id,
            parameters: workflowRun.parameters,
            proxy_location: workflowRun.proxy_location,
          };
          if (workflowRun.max_screenshot_scrolls != null) {
            body.max_screenshot_scrolls = workflowRun.max_screenshot_scrolls;
          }
          if (workflowRun.webhook_callback_url) {
            body.webhook_url = workflowRun.webhook_callback_url;
          }
          return {
            method: "POST",
            url: `${runsApiBaseUrl}/run/workflows`,
            body,
            headers,
          } satisfies ApiCommandOptions;
        }}
        webhookDisabled={!finalized}
        onTestWebhook={() => setReplayOpen(true)}
      />
      <WebhookReplayDialog
        runId={workflowRun.workflow_run_id}
        disabled={!finalized}
        open={replayOpen}
        onOpenChange={setReplayOpen}
        hideTrigger
      />
    </>
  );
}
