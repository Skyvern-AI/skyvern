import { useState } from "react";
import { Share1Icon } from "@radix-ui/react-icons";

import { ApiWebhookActionsMenu } from "@/components/ApiWebhookActionsMenu";
import { StatusBadge } from "@/components/StatusBadge";
import { WebhookReplayDialog } from "@/components/WebhookReplayDialog";
import { useApiCredential } from "@/hooks/useApiCredential";
import { statusIsFinalized } from "@/routes/tasks/types";
import { type ApiCommandOptions } from "@/util/apiCommands";
import { runsApiBaseUrl } from "@/util/env";
import { cn } from "@/util/utils";

import { useWorkflowRunWithWorkflowQuery } from "../../hooks/useWorkflowRunWithWorkflowQuery";
import { useStudioPaneCompact } from "../StudioShellContext";
import { useStudioInspectedRun } from "../useStudioInspectedRun";

/**
 * Left header cluster of the Timeline pane: the inspected run's status pill.
 * Queries dedupe with the pane body via react-query.
 */
export function RunPaneStatusBadge() {
  const { runId } = useStudioInspectedRun();
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery(
    runId ? { workflowRunId: runId } : undefined,
  );
  if (!workflowRun) {
    return null;
  }
  return <StatusBadge status={workflowRun.status} />;
}

/**
 * Right header cluster of the Timeline pane: the API & Webhooks menu for the
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
  const finalized = statusIsFinalized(workflowRun);
  return (
    <>
      <ApiWebhookActionsMenu
        trigger={
          <button
            type="button"
            title={compact ? "API & Webhooks" : undefined}
            aria-label="API & Webhooks"
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium",
              "text-muted-foreground hover:bg-slate-elevation3 hover:text-foreground",
              "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
            )}
          >
            <Share1Icon className="h-4 w-4" />
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
