import { type ReactNode } from "react";
import { Link } from "react-router-dom";

import { type WorkflowRunStatusApiResponseWithWorkflow } from "@/api/types";
import { FailureCategoryBadge } from "@/components/FailureCategoryBadge";
import { StatusBadge } from "@/components/StatusBadge";
import { useBrowserProfileQuery } from "@/routes/browserProfiles/hooks/useBrowserProfileQuery";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { runIsLogicallyFinal } from "@/routes/workflows/workflowRun/runRetryState";
import { WorkflowRunAttemptChip } from "@/components/WorkflowRunAttemptChip";
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
 * The Timeline view's one header: status, run timing, counts, and the search
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
  const finalized = runIsLogicallyFinal(workflowRun);
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
  // failure_category is untyped JSON on the backend, so guard each entry at runtime.
  const signedOutProfileId = (Array.isArray(workflowRun.failure_category)
    ? workflowRun.failure_category
    : []
  ).some((entry) => entry?.reason_code === "saved_profile_signed_out")
    ? workflowRun.browser_profile_id
    : null;
  // A run keeps its profile id after the profile is deleted, and the Refresh
  // dialog cannot open for a deleted profile. Cached data stays successful
  // until a refetch fails, so only a settled lookup from this visit counts.
  const signedOutProfile = useBrowserProfileQuery(
    signedOutProfileId ?? undefined,
    { retry: false, staleTime: 0 },
  );

  return (
    <div className="flex shrink-0 items-start gap-2 [container-name:status] [container-type:inline-size]">
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1 py-1 text-xs">
        {!statusUnavailable ? (
          <StatusBadge status={workflowRun.status} collapsible />
        ) : null}
        {!statusUnavailable ? (
          <WorkflowRunAttemptChip
            attempt={workflowRun.attempt}
            retryPending={workflowRun.retry_pending}
            nextAttemptAt={workflowRun.next_attempt_at}
          />
        ) : null}
        {!statusUnavailable && workflowRun.failure_category?.length ? (
          <FailureCategoryBadge
            failureCategory={workflowRun.failure_category}
          />
        ) : null}
        {!statusUnavailable &&
        signedOutProfileId &&
        signedOutProfile.isSuccess &&
        signedOutProfile.isFetchedAfterMount &&
        !signedOutProfile.isFetching ? (
          <Link
            to={`/browser-profiles/${signedOutProfileId}?refresh=1`}
            className="whitespace-nowrap text-blue-400 hover:text-blue-300"
          >
            Sign in again to refresh
          </Link>
        ) : null}
        {workflowRun.created_at ? (
          <span className="whitespace-nowrap text-muted-foreground">
            Created {compactLocalDateTime(workflowRun.created_at)}
          </span>
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
