import { type WorkflowRunStatusApiResponseWithWorkflow } from "@/api/types";
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

/**
 * Compact meta row atop the Timeline view: the status badge next to the run's
 * duration ("Ran for 50m 12s"), whose hover breaks down the full
 * created/queued/started/finished times; runs the duration can't describe yet
 * (still running, or finalized without both endpoints) fall back to the raw
 * started/finished chips. The run id itself lives in the top bar's "View Run"
 * tab, the browser session/profile ids in the Inputs view, and the counts
 * (blocks/actions/steps/credits) in the timeline's own header.
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
    <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 [container-name:status] [container-type:inline-size]">
      <StatusBadge status={workflowRun.status} collapsible />
      {workflowRun.failure_category?.length ? (
        <FailureCategoryBadge failureCategory={workflowRun.failure_category} />
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
  );
}
