import type { WorkflowBlock, WorkflowParameter } from "../types/workflowTypes";
import type { InsertionPoint } from "@/store/RecordedBlocksStore";
import type { WorkflowCopilotDemonstrationInsertion } from "@/routes/workflows/copilot/workflowCopilotTypes";

type WorkflowNodeLike = {
  id: string;
  type?: string;
  parentId?: string;
  data?: { label?: unknown };
};

type WorkflowEdgeLike = {
  source: string;
  target: string;
};

type SopResult = {
  blocks: Array<WorkflowBlock>;
  parameters: Array<WorkflowParameter>;
};

function resolveAppendInsertionPoint(
  nodes: Array<WorkflowNodeLike>,
  edges: Array<WorkflowEdgeLike>,
): InsertionPoint {
  const trailingAdder = nodes.find(
    (node) => node.type === "nodeAdder" && !node.parentId,
  );
  const incomingEdge = trailingAdder
    ? edges.find((edge) => edge.target === trailingAdder.id)
    : undefined;

  return {
    previous: incomingEdge?.source ?? null,
    next: trailingAdder?.id ?? null,
    parent: undefined,
    connectingEdgeType: "default",
  };
}

function demonstrationInsertionFromNodes(
  nodes: Array<WorkflowNodeLike>,
  insertionPoint: Pick<InsertionPoint, "previous" | "next" | "parent">,
): WorkflowCopilotDemonstrationInsertion | null {
  const labelFor = (nodeId: string | null) => {
    const node = nodes.find((candidate) => candidate.id === nodeId);
    if (!node || node.type === "start" || node.type === "nodeAdder")
      return null;
    const label = node.data?.label;
    return typeof label === "string" && label.length > 0 ? label : null;
  };
  const insertion = {
    after_label: labelFor(insertionPoint.previous),
    before_label: labelFor(insertionPoint.next),
  };
  const parent =
    insertionPoint.parent ??
    nodes.find((node) => node.id === insertionPoint.previous)?.parentId;
  return parent && !insertion.after_label && !insertion.before_label
    ? null
    : insertion;
}

function applySopResultAtCurrentAppend({
  result,
  getNodes,
  getEdges,
  setRecordedBlocks,
}: {
  result: SopResult;
  getNodes: () => Array<WorkflowNodeLike>;
  getEdges: () => Array<WorkflowEdgeLike>;
  setRecordedBlocks: (
    result: SopResult,
    insertionPoint: InsertionPoint,
  ) => void;
}) {
  setRecordedBlocks(
    result,
    resolveAppendInsertionPoint(getNodes(), getEdges()),
  );
}

function resolveWorkspaceAuthoringActionAvailability({
  browserReady,
  isGlobalWorkflow,
  isWorkflowDeleted,
  hasActiveRun,
  isComparing,
  isEditingYaml,
  hasFinallyBlock,
  isRecording,
  isUploadingSOP,
}: {
  browserReady: boolean;
  isGlobalWorkflow: boolean;
  isWorkflowDeleted: boolean;
  hasActiveRun: boolean;
  isComparing: boolean;
  isEditingYaml: boolean;
  hasFinallyBlock: boolean;
  isRecording: boolean;
  isUploadingSOP: boolean;
}) {
  const unavailableReason = isWorkflowDeleted
    ? "This agent is deleted and view-only"
    : isComparing
      ? "Exit history comparison to edit this agent"
      : isEditingYaml
        ? "Switch to Visual mode before adding workflow steps"
        : isGlobalWorkflow
          ? "Make a copy to edit this agent"
          : hasFinallyBlock
            ? "The finally block must remain last. Add steps above it in the editor."
            : isRecording
              ? "Finish recording the current task first"
              : isUploadingSOP
                ? "Wait for the SOP upload to finish"
                : null;
  const recordUnavailableReason =
    unavailableReason ??
    (hasActiveRun ? "Stop the active run before recording a task" : null);

  return {
    canUploadSOP: unavailableReason === null,
    canRecordTask: recordUnavailableReason === null && browserReady,
    unavailableReason: recordUnavailableReason,
  };
}

export {
  applySopResultAtCurrentAppend,
  demonstrationInsertionFromNodes,
  resolveAppendInsertionPoint,
  resolveWorkspaceAuthoringActionAvailability,
};
