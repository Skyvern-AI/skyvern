import { Edge } from "@xyflow/react";
import { useEffect, useRef } from "react";

import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { AppNode } from "../nodes";
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

  const appliedSignatureRef = useRef<string | null>(null);
  const awaitingClearRef = useRef(false);

  useEffect(() => {
    if (!enabled) {
      return;
    }
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

    const {
      nodes: mergedNodes,
      edges: mergedEdges,
      newParameters,
    } = applyRecordedBlocksToGraph({
      nodes,
      edges,
      recordedBlocks,
      recordedInsertionPoint,
      recordedParameters,
      existingParameters: parameters,
    });

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
    recordedBlocks,
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
    return () => {
      useRecordedBlocksStore.getState().clearRecordedBlocks();
    };
  }, []);
}

export { useApplyRecordedBlocks };
