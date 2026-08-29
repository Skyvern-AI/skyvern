import { type ReactNode } from "react";

import { type WorkflowRunStatusApiResponseWithWorkflow } from "@/api/types";
import { FailureCategoryBadge } from "@/components/FailureCategoryBadge";
import { StatusBadge } from "@/components/StatusBadge";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { statusIsFinalized } from "@/routes/tasks/types";
import { type WorkflowRunTimelineItem } from "@/routes/workflows/types/workflowRunTypes";
import { TimelineRunCounts } from "@/routes/workflows/workflowRun/WorkflowRunTimeline";
import { compactLocalDateTime } from "@/util/timeFormat";

import { formatElapsed, formatRunTimesTooltip } from "../runProjections";

type RunSummaryStripProps = {
  workflowRun: WorkflowRunStatusApiResponseWithWorkflow;
  timeline: Array<WorkflowRunTimelineItem> | undefined;
  // Ticking elapsed while the run is live; null once finalized, when the strip
  // reads "Ran for …" from the run's own endpoints instead.
  liveElapsed: string | null;
  statusUnavailable?: boolean;
  // Controls pinned to the right end (the block search), outside the wrap.
  trailing?: ReactNode;
};

/**
 * The Timeline view's one header: status, duration, counts, and the search
 * control on a single line. Run-level facts wrap under width pressure; the
 * trailing controls never move. The run id lives in the top bar's "View Run"
 * tab and the browser session/profile ids in the Inputs view.
 */
export function RunSummaryStrip({
  workflowRun,
  timeline,
  liveElapsed,
  statusUnavailable = false,
  trailing,
}: RunSummaryStripProps) {
  const finalized = statusIsFinalized(workflowRun);
  const ranFor =
    finalized && workflowRun.started_at && workflowRun.finished_at
      ? formatElapsed(workflowRun.started_at, workflowRun.finished_at)
      : null;
  const duration = ranFor ? `Ran for ${ranFor}` : liveElapsed;
  const dateChips = duration
    ? []
    : [
        workflowRun.started_at
          ? `Started ${compactLocalDateTime(workflowRun.started_at)}`
          : null,
        finalized && workflowRun.finished_at
          ? `Finished ${compactLocalDateTime(workflowRun.finished_at)}`
          : null,
      ].filter((chip): chip is string => Boolean(chip));

  return (
    <div className="flex shrink-0 items-start gap-2 [container-name:status] [container-type:inline-size]">
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1 py-1 text-xs">
        {!statusUnavailable ? (
          <StatusBadge status={workflowRun.status} collapsible />
        ) : null}
        {!statusUnavailable && workflowRun.failure_category?.length ? (
          <FailureCategoryBadge
            failureCategory={workflowRun.failure_category}
          />
        ) : null}
        {duration ? (
          <Tooltip>
            <TooltipTrigger asChild>
              {/* tabIndex keeps the breakdown reachable from the keyboard —
                  the chip is plain text, not a control. */}
              <span
                tabIndex={0}
                className="cursor-default whitespace-nowrap text-muted-foreground outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                {ranFor ? (
                  <>
                    Ran for{" "}
                    <span className="tabular-nums text-foreground">
                      {ranFor}
                    </span>
                  </>
                ) : (
                  <span className="tabular-nums text-foreground">
                    {duration}
                  </span>
                )}
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
              className="whitespace-nowrap text-muted-foreground"
            >
              {chip}
            </span>
          ))
        )}
        {timeline ? (
          <TimelineRunCounts workflowRun={workflowRun} timeline={timeline} />
        ) : null}
      </div>
      {trailing ? (
        <div className="flex shrink-0 items-center">{trailing}</div>
      ) : null}
    </div>
  );
}
