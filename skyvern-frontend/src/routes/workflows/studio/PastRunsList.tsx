import { CheckIcon, ReloadIcon } from "@radix-ui/react-icons";
import { Link } from "react-router-dom";

import { StatusBadge } from "@/components/StatusBadge";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import { cn, handleInfiniteScroll } from "@/util/utils";

import {
  getRunAbsoluteTime,
  getRunAgoLabel,
} from "../debugger/recentActivity/runActivity";
import { useInfiniteWorkflowRunsQuery } from "../hooks/useInfiniteWorkflowRunsQuery";
import { useSwitchStudioRun } from "./runSwitchNavigation";
import { useStudioInspectedRun } from "./useStudioInspectedRun";

const PAGE_SIZE = 20;
// While the popover is open, poll past the app-wide 5-minute staleTime so an
// in-flight run's badge and runs started elsewhere surface without a refocus.
const REFRESH_INTERVAL_MS = 10_000;

/**
 * The Past Runs list: an infinite-scrolled history of the workflow's runs. A
 * row switches the studio to that run (useSwitchStudioRun) and opens the run
 * pane via onSelect; the inspected run is highlighted with a check. Distinct
 * from RunTab/RunView, which render one run's detail.
 */
export function PastRunsList({
  open,
  onSelect,
}: {
  open: boolean;
  onSelect: () => void;
}) {
  const workflowPermanentId = useWorkflowPermanentId();
  // The run the studio inspects — the ?wr= run, or the latest-run fallback the
  // Run pane also uses — so the highlighted row matches what the Run pane shows.
  const { runId: inspectedRunId } = useStudioInspectedRun();
  const switchRun = useSwitchStudioRun();
  const { data, isError, hasNextPage, isFetchingNextPage, fetchNextPage } =
    useInfiniteWorkflowRunsQuery({
      workflowPermanentId,
      pageSize: PAGE_SIZE,
      // The list only matters while the popover is open; a closed popover
      // unmounts the content, gating the query and stopping the poll.
      enabled: open,
      refetchInterval: open ? REFRESH_INTERVAL_MS : false,
    });

  const rows = data?.pages.flatMap((page) => page);
  const now = Date.now();

  return (
    <div className="flex max-h-[24rem] min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-between border-b border-border px-3 py-2">
        <span className="text-xs font-medium text-tertiary-foreground">
          Past runs
        </span>
        {rows && rows.length > 0 ? (
          <span className="text-[10px] tabular-nums text-muted-foreground">
            {/* The endpoint returns no total; a next page ("+") makes the count
                a floor, not an exact total. */}
            {rows.length}
            {hasNextPage ? "+" : ""} {rows.length === 1 ? "run" : "runs"}
          </span>
        ) : null}
      </div>
      {rows === undefined ? (
        <div className="flex h-32 items-center justify-center p-4">
          {isError ? (
            <p className="text-sm text-muted-foreground">
              Couldn&apos;t load runs
            </p>
          ) : (
            <ReloadIcon className="size-4 animate-spin text-muted-foreground" />
          )}
        </div>
      ) : rows.length === 0 ? (
        <div className="flex h-32 items-center justify-center p-4">
          <p className="text-sm text-muted-foreground">No runs yet</p>
        </div>
      ) : (
        <div
          className="min-h-0 flex-1 overflow-y-auto p-1"
          onScroll={(event) =>
            handleInfiniteScroll(
              event,
              fetchNextPage,
              hasNextPage,
              isFetchingNextPage,
            )
          }
        >
          <ul className="flex flex-col gap-0.5">
            {rows.map((run) => {
              const isCurrent = run.workflow_run_id === inspectedRunId;
              const ago = getRunAgoLabel(run, now);
              const absolute = getRunAbsoluteTime(run);
              return (
                <li key={run.workflow_run_id}>
                  <button
                    type="button"
                    onClick={() => {
                      // Always ensure the run pane opens — even when the clicked
                      // run is already the URL's run (the pane may be closed).
                      // switchRun only when the run actually changes; onSelect
                      // opens/retargets the pane.
                      if (!isCurrent) {
                        switchRun(run.workflow_run_id);
                      }
                      onSelect();
                    }}
                    aria-current={isCurrent ? "true" : undefined}
                    className={cn(
                      "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors",
                      "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-foreground/40",
                      isCurrent
                        ? "bg-accent dark:bg-white/10"
                        : "hover:bg-muted dark:hover:bg-white/5",
                    )}
                  >
                    <StatusBadge
                      status={run.status}
                      className="shrink-0 md:w-auto"
                    />
                    <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground">
                      {run.workflow_run_id}
                    </span>
                    {ago ? (
                      <span
                        title={absolute ?? undefined}
                        className="shrink-0 text-[10px] tabular-nums text-muted-foreground"
                      >
                        {ago}
                      </span>
                    ) : null}
                    {isCurrent ? (
                      <CheckIcon className="size-4 shrink-0 text-foreground" />
                    ) : null}
                  </button>
                </li>
              );
            })}
          </ul>
          {isFetchingNextPage ? (
            <div className="flex items-center justify-center py-2">
              <ReloadIcon className="size-4 animate-spin text-muted-foreground" />
            </div>
          ) : null}
        </div>
      )}
      {rows && rows.length > 0 && workflowPermanentId ? (
        <div className="shrink-0 border-t border-border">
          {/* Leaves the studio, which unmounts the popover — so, unlike a row,
              this does not call onSelect (that opens the run pane). */}
          <Link
            to={`/agents/${workflowPermanentId}/runs`}
            className="flex items-center justify-center px-3 py-2 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground dark:hover:bg-white/5"
          >
            View all runs
          </Link>
        </div>
      ) : null}
    </div>
  );
}
