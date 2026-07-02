import { useCallback, useEffect, useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { toast } from "@/components/ui/use-toast";
import { statusIsFinalized } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { toDate } from "@/util/utils";

import { useBrowserSessionRateLimit } from "../../hooks/useBrowserSessionRateLimit";
import { useDebugSessionQuery } from "../../hooks/useDebugSessionQuery";
import { useWorkflowQuery } from "../../hooks/useWorkflowQuery";
import {
  useDebugSessionRunsQuery,
  type DebugSessionRun,
} from "../../hooks/useDebugSessionRunsQuery";
import type { WorkflowBlockType } from "../../types/workflowTypes";
import { buildBlockTypeByLabel, getRunActivityKey } from "./runActivity";

export type RecentActivity = {
  /** Debug-session runs sorted ascending by `created_at` (oldest → newest). */
  runs: Array<DebugSessionRun>;
  currentActivityKey: string | null;
  isWorkflowRunning: boolean;
  blockTypeByLabel: Map<string, WorkflowBlockType>;
  navigateToRun: (run: DebugSessionRun) => void;
};

function sortRunsAscending(
  runs: Array<DebugSessionRun>,
): Array<DebugSessionRun> {
  return [...runs].sort((left, right) => {
    const leftTime = toDate(left.created_at ?? "", null)?.getTime() ?? 0;
    const rightTime = toDate(right.created_at ?? "", null)?.getTime() ?? 0;
    return leftTime - rightTime;
  });
}

export function useRecentActivity(): RecentActivity {
  const { blockLabel, workflowPermanentId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data: workflowRun } = useWorkflowRunQuery();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  const { isRateLimited } = useBrowserSessionRateLimit(workflowPermanentId);
  const { data: debugSession } = useDebugSessionQuery({
    workflowPermanentId,
    isRateLimited,
  });
  const { data: debugSessionRuns } = useDebugSessionRunsQuery({
    debugSessionId: debugSession?.debug_session_id,
  });

  const isFinalized = workflowRun ? statusIsFinalized(workflowRun) : null;
  const isWorkflowRunning = isFinalized !== null && !isFinalized;

  const runs = useMemo(
    () => sortRunsAscending(debugSessionRuns?.runs ?? []),
    [debugSessionRuns],
  );

  const blockTypeByLabel = useMemo(
    () => buildBlockTypeByLabel(workflow?.workflow_definition?.blocks),
    [workflow],
  );

  useEffect(() => {
    queryClient.invalidateQueries({
      queryKey: ["debug-session-runs"],
    });
    // Refresh runs only when the active workflow run changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowRun]);

  const navigateToRun = useCallback(
    (run: DebugSessionRun) => {
      if (isWorkflowRunning) {
        return;
      }
      if (!blockTypeByLabel.has(run.block_label)) {
        toast({
          variant: "destructive",
          title: "Block not found",
          description: `The block with label '${run.block_label}' is no longer found in the workflow.`,
        });
        return;
      }
      navigate(
        `/agents/${run.workflow_permanent_id}/${run.workflow_run_id}/${run.block_label}/build`,
      );
    },
    [isWorkflowRunning, blockTypeByLabel, navigate],
  );

  return {
    runs,
    currentActivityKey:
      workflowRun?.workflow_run_id && blockLabel
        ? getRunActivityKey({
            workflow_run_id: workflowRun.workflow_run_id,
            block_label: blockLabel,
          })
        : null,
    isWorkflowRunning,
    blockTypeByLabel,
    navigateToRun,
  };
}
