import { ClockIcon, ExclamationTriangleIcon } from "@radix-ui/react-icons";
import { Link } from "react-router-dom";

import { type WorkflowRunStatusApiResponseWithWorkflow } from "@/api/types";
import { CopyButton } from "@/components/CopyButton";
import { FailureCategoryBadge } from "@/components/FailureCategoryBadge";
import { StatusBadge } from "@/components/StatusBadge";
import { statusIsFinalized } from "@/routes/tasks/types";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";

import { OverviewField } from "./OverviewField";

function StatBlock({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 flex-1 rounded-lg border border-border bg-slate-elevation2 px-2 py-3 text-center">
      <div className="truncate text-base font-semibold text-foreground">
        {value}
      </div>
      <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
    </div>
  );
}

type RunOverviewSectionProps = {
  workflowRun: WorkflowRunStatusApiResponseWithWorkflow;
  actionCount: number;
  elapsed: string;
};

/**
 * Overview body view of the Timeline pane: step / action / credit stat blocks plus
 * the run metadata that used to live in the header's Overview popover.
 */
export function RunOverviewSection({
  workflowRun,
  actionCount,
  elapsed,
}: RunOverviewSectionProps) {
  const finalized = statusIsFinalized(workflowRun);
  const webhookFailureReason =
    workflowRun.task_v2?.webhook_failure_reason ??
    workflowRun.webhook_failure_reason ??
    null;
  const webhookState = !workflowRun.webhook_callback_url
    ? null
    : webhookFailureReason
      ? "failed"
      : finalized
        ? "delivered"
        : "pending";
  const credits =
    (workflowRun.credits_used ?? 0) + (workflowRun.cached_credits_used ?? 0);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex gap-2">
        <StatBlock label="Steps" value={String(workflowRun.total_steps ?? 0)} />
        <StatBlock label="Actions" value={String(actionCount)} />
        <StatBlock label="Credits" value={credits.toLocaleString()} />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={workflowRun.status} />
        {workflowRun.failure_category?.length ? (
          <FailureCategoryBadge
            failureCategory={workflowRun.failure_category}
          />
        ) : null}
      </div>

      {workflowRun.failure_reason ? (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm">
          <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
          <span className="whitespace-pre-wrap break-words text-foreground">
            {workflowRun.failure_reason}
          </span>
        </div>
      ) : null}

      <div className="flex flex-col gap-3">
        {workflowRun.started_at ? (
          <OverviewField label="Started">
            <span
              title={basicTimeFormat(workflowRun.started_at)}
              className="text-xs"
            >
              {basicLocalTimeFormat(workflowRun.started_at)}
            </span>
          </OverviewField>
        ) : null}
        {finalized && workflowRun.finished_at ? (
          <OverviewField label="Finished">
            <span
              title={basicTimeFormat(workflowRun.finished_at)}
              className="text-xs"
            >
              {basicLocalTimeFormat(workflowRun.finished_at)}
            </span>
          </OverviewField>
        ) : null}
        <OverviewField label="Duration">
          <span className="flex items-center gap-1.5 text-xs">
            <ClockIcon className="h-3.5 w-3.5 text-muted-foreground" />
            {elapsed}
          </span>
        </OverviewField>
        <OverviewField label="Run ID">
          <div className="flex items-center gap-1.5">
            <span className="truncate font-mono text-xs">
              {workflowRun.workflow_run_id}
            </span>
            <CopyButton value={workflowRun.workflow_run_id} />
          </div>
        </OverviewField>
        {webhookState ? (
          <OverviewField label="Webhook">
            <span
              className="font-mono text-xs"
              title={webhookFailureReason ?? undefined}
            >
              {webhookState}
            </span>
          </OverviewField>
        ) : null}
        {workflowRun.browser_session_id ? (
          <OverviewField label="Browser session">
            <Link
              to={`/browser-session/${workflowRun.browser_session_id}/stream`}
              className="font-mono text-xs text-studio-accent-2 underline-offset-4 hover:underline"
            >
              {workflowRun.browser_session_id}
            </Link>
          </OverviewField>
        ) : null}
        {workflowRun.browser_profile_id ? (
          <OverviewField label="Browser profile">
            <Link
              to={`/browser-profiles/${workflowRun.browser_profile_id}`}
              className="font-mono text-xs text-studio-accent-2 underline-offset-4 hover:underline"
            >
              {workflowRun.browser_profile_id}
            </Link>
          </OverviewField>
        ) : null}
      </div>
    </div>
  );
}
