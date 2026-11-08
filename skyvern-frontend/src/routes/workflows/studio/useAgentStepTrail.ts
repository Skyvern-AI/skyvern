import { useCallback, useMemo } from "react";

import { useRunViewStore } from "@/store/RunViewStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import type {
  WorkflowRunBlock,
  WorkflowRunTimelineBlockItem,
} from "../types/workflowRunTypes";
import { getRunAttempt } from "../workflowRun/runRetryState";
import { filterTimelineToAttempt } from "../workflowRun/workflowTimelineUtils";
import {
  type AgentStepRef,
  type AgentStepTrailEntry,
  agentStepCallFrameId,
  agentStepTrail,
  findAgentStepItem,
} from "./agentStepTrail";
import { useStudioRunId } from "./useStudioRunId";

export type AgentStepTrailView = {
  item: WorkflowRunTimelineBlockItem;
  entries: Array<AgentStepTrailEntry>;
};

export function useAgentStepTrail(
  ref: AgentStepRef,
): AgentStepTrailView | null {
  const label = "label" in ref ? ref.label : null;
  const workflowRunBlockId =
    "workflowRunBlockId" in ref ? ref.workflowRunBlockId : null;
  const runId = useStudioRunId();
  const { data: retainedTimeline, isPlaceholderData: timelineIsPlaceholder } =
    useWorkflowRunTimelineQuery(runId ? { workflowRunId: runId } : undefined);
  const timeline = timelineIsPlaceholder ? undefined : retainedTimeline;
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery({
    workflowRunId: runId ?? undefined,
  });
  return useMemo(() => {
    if (!timeline) {
      return null;
    }
    const item = findAgentStepItem(
      filterTimelineToAttempt(
        timeline,
        workflowRun?.attempts ?? [],
        getRunAttempt(workflowRun ?? {}),
      ),
      workflowRunBlockId !== null
        ? { workflowRunBlockId }
        : { label: label ?? "" },
    );
    return item
      ? { item, entries: agentStepTrail(item.block, item.children) }
      : null;
  }, [timeline, workflowRun, label, workflowRunBlockId]);
}

export function useSelectAgentStepCall() {
  const runId = useStudioRunId();
  const pinFrame = useRunViewStore((s) => s.pinFrame);
  const closeWorkflowPanel = useWorkflowPanelStore((s) => s.closeWorkflowPanel);
  const setSelectedBlockId = useWorkflowPanelStore((s) => s.setSelectedBlockId);
  return useCallback(
    (agentStep: WorkflowRunBlock, entry: AgentStepTrailEntry) => {
      closeWorkflowPanel();
      setSelectedBlockId(null);
      pinFrame(agentStepCallFrameId(agentStep, entry), null, {
        runId,
        workflowRunBlockId: agentStep.workflow_run_block_id,
        index: entry.call.index,
      });
    },
    [runId, pinFrame, closeWorkflowPanel, setSelectedBlockId],
  );
}

// A selection made on another run is not shown.
export function useSelectedAgentStepCall() {
  const runId = useStudioRunId();
  const selected = useRunViewStore((s) => s.selectedAgentStepCall);
  return selected?.runId === runId ? selected : null;
}

// Needs no run check: a workflow run block id belongs to one run.
export function useAgentStepOwnsSelectedCall(
  workflowRunBlockId: string,
): boolean {
  return useRunViewStore(
    (s) => s.selectedAgentStepCall?.workflowRunBlockId === workflowRunBlockId,
  );
}
