import { Link } from "react-router-dom";

import { type WorkflowRunStatusApiResponseWithWorkflow } from "@/api/types";
import { CopyButton } from "@/components/CopyButton";
import { FailureCategoryBadge } from "@/components/FailureCategoryBadge";
import { StatusBadge } from "@/components/StatusBadge";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { statusIsFinalized } from "@/routes/tasks/types";
import { compactLocalDateTime } from "@/util/timeFormat";

import { formatElapsed, formatRunTimesTooltip } from "../runProjections";

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
 * Compact meta rows atop the Timeline view. Row one: the status badge next to
 * the run's duration ("Ran for 50m 12s"), whose hover breaks down the full
 * created/queued/started/finished times; runs the duration can't describe yet
 * (still running, or finalized without both endpoints) fall back to the raw
 * started/finished chips. Row two, only when the run has one: the browser
 * session/profile as copyable, truncating chips — the run id itself lives in
 * the top bar's "View Run" tab. The counts (blocks/actions/steps/credits)
 * live in the timeline's own header.
 */
export function RunSummaryStrip({ workflowRun }: RunSummaryStripProps) {
  const finalized = statusIsFinalized(workflowRun);
  const ranFor =
    finalized && workflowRun.started_at && workflowRun.finished_at
      ? formatElapsed(workflowRun.started_at, workflowRun.finished_at)
      : null;
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
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 [container-name:status] [container-type:inline-size]">
        <StatusBadge status={workflowRun.status} collapsible />
        {workflowRun.failure_category?.length ? (
          <FailureCategoryBadge
            failureCategory={workflowRun.failure_category}
          />
        ) : null}
        {ranFor ? (
          <Tooltip>
            <TooltipTrigger asChild>
              {/* tabIndex keeps the breakdown reachable from the keyboard —
                  the chip is plain text, not a control. */}
              <span
                tabIndex={0}
                className="cursor-default whitespace-nowrap text-xs text-muted-foreground"
              >
                Ran for {ranFor}
              </span>
            </TooltipTrigger>
            <TooltipContent side="bottom" className="text-left">
              {formatRunTimesTooltip(workflowRun)}
            </TooltipContent>
          </Tooltip>
        ) : (
          dateChips.map((chip) => (
            <span
              key={chip}
              className="whitespace-nowrap text-xs text-muted-foreground"
            >
              {chip}
            </span>
          ))
        )}
      </div>
      {workflowRun.browser_session_id || workflowRun.browser_profile_id ? (
        <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1">
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
      ) : null}
    </div>
  );
}
