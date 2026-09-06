import { useMemo } from "react";
import {
  CheckCircledIcon,
  ExclamationTriangleIcon,
} from "@radix-ui/react-icons";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import { buildBlockStatusMap, runOutcomeFromStatus } from "./runProjections";
import { useStudioRunId } from "./useStudioRunId";

/**
 * Inline per-block run status on the block card. Renders nothing in plain edit
 * mode (no run → no timeline), so it is purely additive over the node.
 *
 * Reports state only. Remediation ("Fix with Copilot", Retry) belongs to the
 * Run pane, which has the failure reason and the copilot bridge; this chip has
 * neither and must not promise them.
 */
export function BlockStatusBar({ blockLabel }: { blockLabel: string }) {
  const runId = useStudioRunId();
  const { data: retainedTimeline, isPlaceholderData: timelineIsPlaceholder } =
    useWorkflowRunTimelineQuery(runId ? { workflowRunId: runId } : undefined);
  const timeline = timelineIsPlaceholder ? undefined : retainedTimeline;
  const statusMap = useMemo(() => buildBlockStatusMap(timeline), [timeline]);
  const state = statusMap[blockLabel];
  if (!state || !state.status) {
    return null;
  }
  const outcome = runOutcomeFromStatus(state.status);

  if (outcome === "running") {
    return (
      <div className="flex items-center gap-2 rounded-md bg-warning/15 px-3 py-2 text-xs font-medium text-warning">
        <span className="h-2 w-2 animate-pulse rounded-full bg-warning" />
        Running…
      </div>
    );
  }
  if (outcome === "failed") {
    return (
      <div className="flex items-center gap-2 rounded-md bg-destructive/15 px-3 py-2 text-xs font-medium text-destructive">
        <ExclamationTriangleIcon className="h-3.5 w-3.5" />
        Failed
      </div>
    );
  }
  if (outcome === "success") {
    return (
      <div className="flex items-center gap-2 rounded-md bg-success/15 px-3 py-2 text-xs font-medium text-success">
        <CheckCircledIcon className="h-3.5 w-3.5" />
        Completed
        {state.actionCount > 0 ? ` · ${state.actionCount} actions` : null}
      </div>
    );
  }
  return null;
}
