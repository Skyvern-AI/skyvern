/**
 * THe debugger has an underlying debug_session_id. Block runs that occur within
 * same debug session are grouped together. We will show them with this component.
 */

import { useEffect, useRef } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { Tip } from "@/components/Tip";
import { statusIsFinalized } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { cn, formatMs, toDate } from "@/util/utils";

import { useDebugSessionQuery } from "../hooks/useDebugSessionQuery";
import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
import {
  useDebugSessionRunsQuery,
  type DebugSessionRun,
} from "../hooks/useDebugSessionRunsQuery";
import { toast } from "@/components/ui/use-toast";

function DebuggerBlockRuns() {
  const { workflowPermanentId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const { data: workflowRun } = useWorkflowRunQuery();
  const { data: workflow } = useWorkflowQuery({
    workflowPermanentId,
  });
  const { data: debugSession } = useDebugSessionQuery({
    workflowPermanentId,
  });
  const { data: debugSessionRuns } = useDebugSessionRunsQuery({
    debugSessionId: debugSession?.debug_session_id,
  });

  const numRuns = debugSessionRuns?.runs.length ?? 0;
  const isFinalized = workflowRun ? statusIsFinalized(workflowRun) : null;
  const isRunning = isFinalized !== null && !isFinalized;

  const handleClick = (run: DebugSessionRun) => {
    if (isRunning) {
      return;
    }

    const blockLabel = run.block_label;
    const workflowDefinition = workflow?.workflow_definition;
    const blocks = workflowDefinition?.blocks ?? [];
    const block = blocks.find((b) => b.label === blockLabel);

    if (!block) {
      toast({
        variant: "destructive",
        title: "Block not found",
        description: `The block with label '${blockLabel}' is no longer found in the workflow.`,
      });

      return;
    }

    navigate(
      `/workflows/${run.workflow_permanent_id}/${run.workflow_run_id}/${blockLabel}/debug`,
    );
  };

  useEffect(() => {
    queryClient.invalidateQueries({
      queryKey: ["debug-session-runs"],
    });
    // We only want to run this when the workflowRun changes, not on every render
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowRun]);

  useEffect(() => {
    if (scrollContainerRef.current) {
      scrollContainerRef.current.scrollLeft =
        scrollContainerRef.current.scrollWidth;
    }
  }, [debugSessionRuns]);

  if (numRuns <= 1) {
    return null;
  }

  return (
    <div className="relative flex w-full items-center justify-center gap-2 opacity-80 hover:opacity-100">
      <div
        ref={scrollContainerRef}
        className="flex max-w-[7rem] gap-2 overflow-x-auto rounded-full bg-[#020617] p-2 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      >
        {[...(debugSessionRuns?.runs ?? [])].reverse().map((run) => {
          const dt = toDate(run.created_at ?? "", null);
          const ago = dt ? formatMs(Date.now() - dt.getTime()).ago : null;
          return (
            <Tip
              key={run.workflow_run_id}
              content={
                ago
                  ? `${run.block_label} [${run.status}] (${ago})`
                  : `${run.block_label} [${run.status}]`
              }
            >
              <div
                className={cn(
                  "h-[1rem] w-[1rem] flex-shrink-0 rounded-full border border-white/50 hover:border-white/80",
                  {
                    "cursor-pointer": !isRunning,
                  },
                  {
                    "animate-spin border-dashed [animation-duration:_2s]":
                      run.status === "running" && isRunning,
                    "border-[red] opacity-50 hover:border-[red] hover:opacity-100":
                      run.status === "failed",
                  },
                )}
                onClick={() => handleClick(run)}
              />
            </Tip>
          );
        })}
      </div>
    </div>
  );
}

export { DebuggerBlockRuns };
