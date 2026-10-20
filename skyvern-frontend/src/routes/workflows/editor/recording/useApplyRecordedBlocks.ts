import { Edge } from "@xyflow/react";
import { useEffect, useRef } from "react";

import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import {
  reconcileYamlDraftAfterGraphChange,
  selectEditorMutationLocked,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import { AppNode } from "../nodes";
import { resolveAppendInsertionPoint } from "../workspaceAuthoringActions";
import { applyRecordedBlocksToGraph } from "./applyRecordedBlocksToGraph";

type UseApplyRecordedBlocksArgs = {
  enabled: boolean;
  nodes: Array<AppNode>;
  edges: Array<Edge>;
  doLayout: (nodes: Array<AppNode>, edges: Array<Edge>) => void;
};

function useApplyRecordedBlocks({
  enabled,
  nodes,
  edges,
  doLayout,
}: UseApplyRecordedBlocksArgs) {
  const recordedOwner = useRecordedBlocksStore((state) => state.owner);
  const recordedBlocks = useRecordedBlocksStore((state) => state.blocks);
  const recordedParameters = useRecordedBlocksStore(
    (state) => state.parameters,
  );
  const recordedInsertionPoint = useRecordedBlocksStore(
    (state) => state.insertionPoint,
  );
  const applicationNonce = useRecordedBlocksStore(
    (state) => state.applicationNonce,
  );
  const clearRecordedBlocks = useRecordedBlocksStore(
    (state) => state.clearRecordedBlocks,
  );
  const parameters = useWorkflowParametersStore((state) => state.parameters);
  const isLocked = useWorkflowYamlEditorStore(selectEditorMutationLocked);

  const appliedSignatureRef = useRef<string | null>(null);
  const awaitingClearRef = useRef(false);

  useEffect(() => {
    const current = useWorkflowYamlEditorStore.getState();
    if (
      !enabled ||
      isLocked ||
      current.commitInProgress ||
      current.copilotAcceptance
    ) {
      return;
    }
    if (!recordedOwner?.active || recordedOwner !== current.editorOwner) return;
    if (!recordedBlocks?.length || !recordedInsertionPoint) {
      return;
    }

    const signature = [
      String(applicationNonce),
      recordedInsertionPoint.previous ?? "",
      recordedInsertionPoint.next ?? "",
      recordedInsertionPoint.parent ?? "",
      recordedInsertionPoint.connectingEdgeType,
      recordedBlocks.length,
      recordedBlocks
        .map((block) => `${block.block_type}:${block.label ?? ""}`)
        .join(","),
    ].join("|");

    if (appliedSignatureRef.current === signature && awaitingClearRef.current) {
      return;
    }

    // A remounted workflow has new canvas IDs. Its upload can still append,
    // but must not create edges pointing to the disposed canvas.
    const insertionPoint =
      (!recordedInsertionPoint.previous && !recordedInsertionPoint.next) ||
      [
        recordedInsertionPoint.previous,
        recordedInsertionPoint.next,
        recordedInsertionPoint.parent,
      ].some((id) => id && !nodes.some((node) => node.id === id))
        ? resolveAppendInsertionPoint(nodes, edges)
        : recordedInsertionPoint;
    const {
      nodes: mergedNodes,
      edges: mergedEdges,
      newParameters,
    } = applyRecordedBlocksToGraph({
      nodes,
      edges,
      recordedBlocks,
      recordedInsertionPoint: insertionPoint,
      recordedParameters,
      existingParameters: parameters,
    });

    useWorkflowTitleStore.getState().recordCopilotGraphEdit();
    reconcileYamlDraftAfterGraphChange();
    useWorkflowHasChangesStore.getState().setHasChanges(true);
    doLayout(mergedNodes, mergedEdges);

    if (newParameters.length > 0) {
      const workflowParametersStore = useWorkflowParametersStore.getState();
      workflowParametersStore.setParameters([
        ...workflowParametersStore.parameters,
        ...newParameters,
      ]);
    }

    appliedSignatureRef.current = signature;
    awaitingClearRef.current = true;
    // nodes/edges/parameters are read from the render that saw the new store
    // payload; listing them as deps would re-apply on every canvas edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    enabled,
    isLocked,
    recordedBlocks,
    recordedOwner,
    recordedInsertionPoint,
    applicationNonce,
    doLayout,
  ]);

  useEffect(() => {
    if (!awaitingClearRef.current) {
      return;
    }
    awaitingClearRef.current = false;
    clearRecordedBlocks();
    appliedSignatureRef.current = null;
  }, [nodes, edges, clearRecordedBlocks]);

  // The bridge dies with its consumer: this hook is the only thing that applies
  // recorded blocks, so if it unmounts while a payload is still pending (e.g.
  // the user navigates away right after a commit), the blocks must not survive
  // to be applied to whichever workflow canvas mounts next.
  useEffect(() => {
    const owner = useWorkflowYamlEditorStore.getState().editorOwner;
    return () => {
      const recorded = useRecordedBlocksStore.getState();
      if (!recorded.owner || recorded.owner === owner)
        recorded.clearRecordedBlocks();
    };
  }, []);
}

export { useApplyRecordedBlocks };
