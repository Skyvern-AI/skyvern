import { PlusIcon } from "@radix-ui/react-icons";
import {
  BaseEdge,
  EdgeLabelRenderer,
  EdgeProps,
  getBezierPath,
  useNodes,
} from "@xyflow/react";

import { Button } from "@/components/ui/button";
import {
  BranchContext,
  useWorkflowPanelStore,
} from "@/store/WorkflowPanelStore";
import { useProcessRecordingMutation } from "@/routes/browserSessions/hooks/useProcessRecordingMutation";
import { useDebugStore } from "@/store/useDebugStore";
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useSettingsStore } from "@/store/SettingsStore";
import { cn } from "@/util/utils";

import { REACT_FLOW_EDGE_Z_INDEX } from "../constants";
import type { NodeBaseData } from "../nodes/types";
import { WorkflowAddMenu } from "../WorkflowAddMenu";
import { WorkflowAdderBusy } from "../WorkflowAdderBusy";

function EdgeWithAddButton({
  source,
  target,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  style = {},
  markerEnd,
}: EdgeProps) {
  const nodes = useNodes();
  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });
  const debugStore = useDebugStore();
  const recordingStore = useRecordingStore();
  const settingsStore = useSettingsStore();
  const workflowStatePanel = useWorkflowPanelStore();
  const setRecordedBlocks = useRecordedBlocksStore(
    (state) => state.setRecordedBlocks,
  );
  const setWorkflowPanelState = useWorkflowPanelStore(
    (state) => state.setWorkflowPanelState,
  );
  const processRecordingMutation = useProcessRecordingMutation({
    browserSessionId: settingsStore.browserSessionId,
    onSuccess: (result) => {
      setRecordedBlocks(result, {
        previous: source,
        next: target,
        parent: sourceNode?.parentId,
        connectingEdgeType: "edgeWithAddButton",
      });
    },
  });

  const isProcessing = processRecordingMutation.isPending;

  const sourceNode = nodes.find((node) => node.id === source);

  const isBusy =
    (isProcessing || recordingStore.isRecording) &&
    debugStore.isDebugMode &&
    settingsStore.isUsingABrowser &&
    workflowStatePanel.workflowPanelState.data?.previous === source &&
    workflowStatePanel.workflowPanelState.data?.next === target &&
    workflowStatePanel.workflowPanelState.data?.parent ===
      (sourceNode?.parentId || undefined);

  const isDisabled = !isBusy && recordingStore.isRecording;

  const deriveBranchContext = (): BranchContext | undefined => {
    if (
      sourceNode &&
      "data" in sourceNode &&
      (sourceNode.data as NodeBaseData).conditionalBranchId &&
      (sourceNode.data as NodeBaseData).conditionalNodeId
    ) {
      const sourceData = sourceNode.data as NodeBaseData;
      return {
        conditionalNodeId: sourceData.conditionalNodeId!,
        conditionalLabel: sourceData.conditionalLabel ?? sourceData.label,
        branchId: sourceData.conditionalBranchId!,
        mergeLabel: sourceData.conditionalMergeLabel ?? null,
      };
    }

    // If source node doesn't have branch context, check if it's inside a conditional block
    // (e.g., StartNode or NodeAdderNode inside a conditional)
    if (sourceNode?.parentId) {
      const parentNode = nodes.find((n) => n.id === sourceNode.parentId);
      if (parentNode?.type === "conditional" && "data" in parentNode) {
        const conditionalData = parentNode.data as {
          activeBranchId: string | null;
          branches: Array<{ id: string }>;
          label: string;
          mergeLabel: string | null;
        };
        const activeBranchId = conditionalData.activeBranchId;
        const activeBranch = conditionalData.branches?.find(
          (b) => b.id === activeBranchId,
        );

        if (activeBranch) {
          return {
            conditionalNodeId: parentNode.id,
            conditionalLabel: conditionalData.label,
            branchId: activeBranch.id,
            mergeLabel: conditionalData.mergeLabel ?? null,
          };
        }
      }
    }

    return undefined;
  };

  const updateWorkflowPanelState = (
    active: boolean,
    branchContext?: BranchContext,
  ) => {
    setWorkflowPanelState({
      active,
      content: "nodeLibrary",
      data: {
        previous: source,
        next: target,
        parent: branchContext?.conditionalNodeId ?? sourceNode?.parentId,
        connectingEdgeType: "edgeWithAddButton",
        branchContext,
      },
    });
  };

  const onAdd = () => {
    if (isDisabled) {
      return;
    }
    const branchContext = deriveBranchContext();
    updateWorkflowPanelState(true, branchContext);
  };

  const onRecord = () => {
    if (recordingStore.isRecording) {
      recordingStore.setIsRecording(false);
    } else {
      recordingStore.setIsRecording(true);
      updateWorkflowPanelState(false);
    }
  };

  const onEndRecord = () => {
    if (recordingStore.isRecording) {
      recordingStore.setIsRecording(false);
    }

    processRecordingMutation.mutate();
  };

  const adder = (
    <Button
      size="icon"
      className={cn("h-4 w-4 rounded-full transition-all hover:scale-150", {
        "cursor-not-allowed bg-[grey] hover:scale-100 hover:bg-[grey] active:bg-[grey]":
          isDisabled,
      })}
      onClick={() => onAdd()}
    >
      <PlusIcon />
    </Button>
  );

  const menu = (
    <WorkflowAddMenu
      buttonSize="25px"
      radius="40px"
      onAdd={onAdd}
      onRecord={onRecord}
    >
      {adder}
    </WorkflowAddMenu>
  );

  const busy = (
    <WorkflowAdderBusy
      color={isProcessing ? "white" : "red"}
      operation={isProcessing ? "processing" : "recording"}
      size="small"
      onComplete={() => {
        onEndRecord();
      }}
    >
      {adder}
    </WorkflowAdderBusy>
  );

  return (
    <>
      <BaseEdge path={edgePath} markerEnd={markerEnd} style={style} />
      <EdgeLabelRenderer>
        <div
          style={{
            position: "absolute",
            transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)`,
            fontSize: 12,
            // everything inside EdgeLabelRenderer has no pointer events by default
            // if you have an interactive element, set pointer-events: all
            pointerEvents: "all",
            zIndex: REACT_FLOW_EDGE_Z_INDEX + 1, // above the edge
          }}
          className="nodrag nopan"
        >
          {isBusy ? busy : isDisabled ? adder : menu}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

export { EdgeWithAddButton };
