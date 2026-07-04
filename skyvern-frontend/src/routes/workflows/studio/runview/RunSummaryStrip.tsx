import { ClockIcon } from "@radix-ui/react-icons";
import { Link } from "react-router-dom";

import { type WorkflowRunStatusApiResponseWithWorkflow } from "@/api/types";
import { CopyButton } from "@/components/CopyButton";
import { FailureCategoryBadge } from "@/components/FailureCategoryBadge";
import { StatusBadge } from "@/components/StatusBadge";
import { statusIsFinalized } from "@/routes/tasks/types";
import { basicLocalTimeFormat } from "@/util/timeFormat";

type RunSummaryStripProps = {
  workflowRun: WorkflowRunStatusApiResponseWithWorkflow;
  elapsed: string;
};

function RunResourceLink({
  label,
  id,
  to,
}: {
  label: string;
  id: string;
  to: string;
}) {
  return (
    <span className="flex min-w-0 items-center gap-1">
      <Link
        to={to}
        title={label}
        className="truncate font-mono text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
      >
        {id}
      </Link>
      <CopyButton value={id} />
    </span>
  );
}

/**
 * One meta line atop the Timeline view: status · duration · run id. Counts
 * (blocks/actions/steps/credits) live in the timeline's own header row, and
 * started/finished timestamps ride the duration tooltip, so the strip never
 * eats the timeline's vertical budget. Runs with a browser session/profile get
 * a second quiet line linking them (same targets as the legacy run header).
 */
export function RunSummaryStrip({
  workflowRun,
  elapsed,
}: RunSummaryStripProps) {
  const finalized = statusIsFinalized(workflowRun);
  const timesTooltip = [
    workflowRun.started_at
      ? `Started ${basicLocalTimeFormat(workflowRun.started_at)}`
      : null,
    finalized && workflowRun.finished_at
      ? `Finished ${basicLocalTimeFormat(workflowRun.finished_at)}`
      : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="flex shrink-0 flex-col gap-2">
      <div className="flex min-w-0 items-center gap-2">
        <StatusBadge status={workflowRun.status} />
        {workflowRun.failure_category?.length ? (
          <FailureCategoryBadge
            failureCategory={workflowRun.failure_category}
          />
        ) : null}
        <span
          title={timesTooltip || undefined}
          className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground"
        >
          <ClockIcon className="h-3.5 w-3.5" />
          {elapsed}
        </span>
        <span className="min-w-0 flex-1" />
        <span className="truncate font-mono text-xs text-muted-foreground">
          {workflowRun.workflow_run_id}
        </span>
        <CopyButton value={workflowRun.workflow_run_id} />
      </div>
      {workflowRun.browser_session_id || workflowRun.browser_profile_id ? (
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
          {workflowRun.browser_session_id ? (
            <RunResourceLink
              label="Browser session"
              id={workflowRun.browser_session_id}
              to={`/browser-session/${workflowRun.browser_session_id}/stream`}
            />
          ) : null}
          {workflowRun.browser_profile_id ? (
            <RunResourceLink
              label="Browser profile"
              id={workflowRun.browser_profile_id}
              to={`/browser-profiles/${workflowRun.browser_profile_id}`}
            />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
