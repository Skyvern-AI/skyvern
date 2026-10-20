import type { CopilotProposalMetadata } from "../copilot/workflowCopilotTypes";
import type {
  WorkflowApiResponse,
  WorkflowSettings,
} from "../types/workflowTypes";

import type { Edge } from "@xyflow/react";
import type { AppNode } from "./nodes";
import type { ParametersState } from "./types";
import type { CopilotReviewStatus } from "./panels/WorkflowComparisonPanel";
import type { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import type { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import type { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowSnapshotStore } from "@/store/WorkflowSnapshotStore";
import type { useNodeCollapseStore } from "./collapse/useNodeCollapseStore";
import { cloneSnapshot } from "./hooks/workflowHistoryState";
import { restoreCollapseVisibility } from "./collapse/applyDescendantCollapseVisibility";

export type EditorStateSnapshot = {
  workflowPermanentId: string;
  nodes: AppNode[];
  edges: Edge[];
  parameters: ParametersState;
  parameterBaseline?: WorkflowApiResponse["workflow_definition"]["parameters"];
  title: string;
  titleHasBeenGenerated: boolean;
  description: string | null;
  hasChanges: boolean;
  saveGeneration: number;
  workflowSnapshot?: Pick<
    ReturnType<typeof useWorkflowSnapshotStore.getState>,
    "snapshot" | "contentDirty" | "userHasEdited"
  >;
};

export type RestoreResult =
  | "restored"
  | "refused-locked"
  | "refused-stale-workflow";

export function bindCopilotReviewClose(
  reject: () => Promise<boolean>,
  close: (status: CopilotReviewStatus) => void | Promise<void>,
): (status: CopilotReviewStatus) => Promise<void> {
  return async (status) => {
    if (status === "reject" && !(await reject())) return;
    await close(status);
  };
}

export function captureEditorState(
  inputs: EditorStateSnapshot,
): EditorStateSnapshot {
  const { snapshot, contentDirty, userHasEdited } =
    useWorkflowSnapshotStore.getState();
  return {
    ...inputs,
    workflowSnapshot: structuredClone({
      snapshot,
      contentDirty,
      userHasEdited,
    }),
    ...cloneSnapshot(inputs.nodes, inputs.edges),
    parameters: structuredClone(inputs.parameters),
    ...(inputs.parameterBaseline
      ? { parameterBaseline: structuredClone(inputs.parameterBaseline) }
      : {}),
  };
}

type RestoreDependencies = {
  workflowPermanentId: string;
  setNodes: (nodes: AppNode[]) => void;
  setEdges: (edges: Edge[]) => void;
  parametersStore: ReturnType<typeof useWorkflowParametersStore.getState>;
  titleStore: ReturnType<typeof useWorkflowTitleStore.getState>;
  changesStore: ReturnType<typeof useWorkflowHasChangesStore.getState>;
  collapseStore: ReturnType<typeof useNodeCollapseStore.getState>;
  restoreOwnership: (workflowPermanentId: string) => void;
  scheduleLayout: () => void;
  isLockedByOther: () => boolean;
};

export function restoreEditorState(
  snapshot: EditorStateSnapshot,
  deps: RestoreDependencies,
): RestoreResult {
  if (snapshot.workflowPermanentId !== deps.workflowPermanentId)
    return "refused-stale-workflow";
  if (deps.isLockedByOther()) return "refused-locked";
  if (!deps.parametersStore.setParameters(structuredClone(snapshot.parameters)))
    return "refused-locked";
  const graph = cloneSnapshot(snapshot.nodes, snapshot.edges);
  deps.setNodes(
    restoreCollapseVisibility(
      graph.nodes,
      snapshot.workflowPermanentId,
      deps.collapseStore.collapsed,
    ),
  );
  deps.setEdges(graph.edges);
  deps.titleStore.restoreTitle(snapshot.title, snapshot.titleHasBeenGenerated);
  deps.titleStore.setDescriptionFromWorkflow(snapshot.description);
  deps.restoreOwnership(snapshot.workflowPermanentId);
  deps.changesStore.setHasChanges(
    snapshot.hasChanges ||
      (deps.changesStore.saveGenerationsByWorkflow[
        snapshot.workflowPermanentId
      ] ?? 0) > snapshot.saveGeneration,
  );
  if (snapshot.workflowSnapshot) {
    useWorkflowSnapshotStore.setState(
      structuredClone(snapshot.workflowSnapshot),
    );
  }
  deps.scheduleLayout();
  return "restored";
}
export type CopilotAcceptSnapshot = {
  workflowPermanentId: string;
  chatId: string;
  acceptChatId: string;
  acceptAttempt: Pick<
    CopilotProposalMetadata,
    "owner_turn_id" | "revision" | "disposition"
  > | null;
  baseline?: WorkflowApiResponse;
  preservedSettings?: WorkflowSettings;
  waitingForUnlock: boolean;
  terminalConfirmed: boolean;
  yaml: { draft: string; entrySnapshot: string } | null;
};
