import { Link } from "react-router-dom";

import { type WorkflowRunStatusApiResponseWithWorkflow } from "@/api/types";
import { CopyButton } from "@/components/CopyButton";
import { FailureCategoryBadge } from "@/components/FailureCategoryBadge";
import { StatusBadge } from "@/components/StatusBadge";
import { statusIsFinalized } from "@/routes/tasks/types";
import { compactLocalDateTime } from "@/util/timeFormat";

type RunSummaryStripProps = {
  workflowRun: WorkflowRunStatusApiResponseWithWorkflow;
};

function RunIdChip({
  label,
  id,
  to,
}: {
  label: string;
  id: string;
  to?: string;
}) {
  return (
    <span className="flex min-w-0 max-w-full items-center gap-1">
      {to ? (
        <Link
          to={to}
          title={label}
          className="min-w-0 truncate font-mono text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
        >
          {id}
        </Link>
      ) : (
        <span
          title={id}
          className="min-w-0 truncate font-mono text-xs text-muted-foreground"
        >
          {id}
        </span>
      )}
      <CopyButton value={id} />
    </span>
  );
}

/**
 * Two compact meta rows atop the Timeline view. Row one: the status badge next
 * to the run's started/finished timestamps. Row two: the run id — plus its
 * browser session/profile when present — as copyable, truncating chips. Both
 * rows wrap gracefully under width pressure (dates stack rather than mangle
 * mid-word; ids truncate with the full value on hover/copy). Elapsed time and
 * the counts (blocks/actions/steps/credits) live in the timeline's own header.
 */
export function RunSummaryStrip({ workflowRun }: RunSummaryStripProps) {
  const finalized = statusIsFinalized(workflowRun);
  const dateChips = [
    workflowRun.started_at
      ? `Started ${compactLocalDateTime(workflowRun.started_at)}`
      : null,
    finalized && workflowRun.finished_at
      ? `Finished ${compactLocalDateTime(workflowRun.finished_at)}`
      : null,
  ].filter((chip): chip is string => Boolean(chip));

  return (
    <div className="flex shrink-0 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <StatusBadge status={workflowRun.status} />
        {workflowRun.failure_category?.length ? (
          <FailureCategoryBadge
            failureCategory={workflowRun.failure_category}
          />
        ) : null}
        {dateChips.map((chip) => (
          <span
            key={chip}
            className="whitespace-nowrap text-xs text-muted-foreground"
          >
            {chip}
          </span>
        ))}
      </div>
      <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1">
        <RunIdChip label="Run" id={workflowRun.workflow_run_id} />
        {workflowRun.browser_session_id ? (
          <RunIdChip
            label="Browser session"
            id={workflowRun.browser_session_id}
            to={`/browser-session/${workflowRun.browser_session_id}/stream`}
          />
        ) : null}
        {workflowRun.browser_profile_id ? (
          <RunIdChip
            label="Browser profile"
            id={workflowRun.browser_profile_id}
            to={`/browser-profiles/${workflowRun.browser_profile_id}`}
          />
        ) : null}
      </div>
    </div>
  );
}
