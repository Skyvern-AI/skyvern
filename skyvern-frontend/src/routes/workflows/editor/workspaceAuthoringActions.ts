import { toast } from "@/components/ui/use-toast";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import type { WorkflowCopilotDemonstrationInsertion } from "@/routes/workflows/copilot/workflowCopilotTypes";

type InsertionPoint = {
  previous: string | null;
  next: string | null;
  parent?: string;
  connectingEdgeType: string;
};

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

function blockLabelFor(
  nodes: Array<WorkflowNodeLike>,
  nodeId: string | null | undefined,
): string | null {
  const node = nodes.find((candidate) => candidate.id === nodeId);
  if (!node || node.type === "start" || node.type === "nodeAdder") return null;
  const label = node.data?.label;
  return typeof label === "string" && label.length > 0 ? label : null;
}

function demonstrationInsertionFromNodes(
  nodes: Array<WorkflowNodeLike>,
  insertionPoint: Pick<InsertionPoint, "previous" | "next" | "parent">,
): WorkflowCopilotDemonstrationInsertion | null {
  const insertion = {
    after_label: blockLabelFor(nodes, insertionPoint.previous),
    before_label: blockLabelFor(nodes, insertionPoint.next),
  };
  const parent =
    insertionPoint.parent ??
    nodes.find((node) => node.id === insertionPoint.previous)?.parentId;
  return parent && !insertion.after_label && !insertion.before_label
    ? null
    : insertion;
}

function sopAttachmentDraft(
  insertion: WorkflowCopilotDemonstrationInsertion | null = null,
  containerLabel: string | null = null,
): string {
  if (insertion?.after_label)
    return `Add the steps from the attached SOP after the ${insertion.after_label} block.`;
  if (insertion?.before_label)
    return `Add the steps from the attached SOP before the ${insertion.before_label} block.`;
  if (containerLabel)
    return `Add the steps from the attached SOP inside the ${containerLabel} block.`;
  return "Add the steps from the attached SOP to this agent.";
}

function stageSopFile(file: File, draft: string) {
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    toast({
      variant: "destructive",
      title: "Invalid file type",
      description: "Please select a PDF file",
    });
    return;
  }
  // Read at pick time: Copilot may have asked a question or started finishing a recording while
  // the file picker was open.
  const stage = useCopilotActionStore.getState().stageAttachment;
  if (!stage) {
    toast({
      title: "Copilot can't take a file right now",
      description:
        "Answer its question or wait for it to finish, then try again.",
    });
    return;
  }
  stage(file, draft);
}

function resolveWorkspaceAuthoringActionAvailability({
  browserReady,
  isGlobalWorkflow,
  isWorkflowDeleted,
  hasActiveRun,
  isComparing,
  isEditingYaml,
  hasFinallyBlock,
}: {
  browserReady: boolean;
  isGlobalWorkflow: boolean;
  isWorkflowDeleted: boolean;
  hasActiveRun: boolean;
  isComparing: boolean;
  isEditingYaml: boolean;
  hasFinallyBlock: boolean;
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
            : null;
  const recordUnavailableReason =
    unavailableReason ??
    (hasActiveRun ? "Stop the active run before recording a task" : null);

  return {
    canRecordTask: recordUnavailableReason === null && browserReady,
    unavailableReason: recordUnavailableReason,
  };
}

export {
  blockLabelFor,
  demonstrationInsertionFromNodes,
  resolveAppendInsertionPoint,
  resolveWorkspaceAuthoringActionAvailability,
  sopAttachmentDraft,
  stageSopFile,
};
