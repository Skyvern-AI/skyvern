import { PlusIcon } from "@radix-ui/react-icons";
import { Handle, NodeProps, Position, useEdges, useNodes } from "@xyflow/react";

import { useProcessRecordingMutation } from "@/routes/browserSessions/hooks/useProcessRecordingMutation";
import { useDebugStore } from "@/store/useDebugStore";
import {
  BranchContext,
  useWorkflowPanelStore,
} from "@/store/WorkflowPanelStore";
import type { NodeBaseData } from "../types";
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useSettingsStore } from "@/store/SettingsStore";
import { cn } from "@/util/utils";

import type { NodeAdderNode } from "./types";
import { WorkflowAddMenu } from "../../WorkflowAddMenu";
import { WorkflowAdderBusy } from "../../WorkflowAdderBusy";

function NodeAdderNode({ id, parentId }: NodeProps<NodeAdderNode>) {
  const edges = useEdges();
  const nodes = useNodes();
  const debugStore = useDebugStore();
  const recordingStore = useRecordingStore();
  const settingsStore = useSettingsStore();
  const setWorkflowPanelState = useWorkflowPanelStore(
    (state) => state.setWorkflowPanelState,
  );
  const workflowStatePanel = useWorkflowPanelStore();
  const setRecordedBlocks = useRecordedBlocksStore(
    (state) => state.setRecordedBlocks,
  );

  const deriveBranchContext = (previousNodeId: string | undefined) => {
    const previousNode = nodes.find((node) => node.id === previousNodeId);
    if (
      previousNode &&
      "data" in previousNode &&
      (previousNode.data as NodeBaseData).conditionalBranchId &&
      (previousNode.data as NodeBaseData).conditionalNodeId
    ) {
      const prevData = previousNode.data as NodeBaseData;
      return {
        conditionalNodeId: prevData.conditionalNodeId!,
        conditionalLabel: prevData.conditionalLabel ?? prevData.label,
        branchId: prevData.conditionalBranchId!,
        mergeLabel: prevData.conditionalMergeLabel ?? null,
      } satisfies BranchContext;
    }

    // If previous node doesn't have branch context, check if this NodeAdderNode is inside a conditional block
    if (parentId) {
      const parentNode = nodes.find((n) => n.id === parentId);
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
          } satisfies BranchContext;
        }
      }
    }

    return undefined;
  };

  // Find the edge that targets this NodeAdder
  // If inside a conditional, find the edge for the active branch
  const previous = (() => {
    const incomingEdges = edges.filter((edge) => edge.target === id);

    // If inside a conditional, filter by active branch
    if (parentId) {
      const parentNode = nodes.find((n) => n.id === parentId);
      if (parentNode?.type === "conditional" && "data" in parentNode) {
        const conditionalData = parentNode.data as {
          activeBranchId: string | null;
        };
        const activeBranchId = conditionalData.activeBranchId;

        // Find edge for active branch
        const branchEdge = incomingEdges.find((edge) => {
          const edgeData = edge.data as
            | { conditionalBranchId?: string }
            | undefined;
          return edgeData?.conditionalBranchId === activeBranchId;
        });

        if (branchEdge) {
          return branchEdge.source;
        }
      }
    }

    // Otherwise return the first edge
    return incomingEdges[0]?.source;
  })();

  const processRecordingMutation = useProcessRecordingMutation({
    browserSessionId: settingsStore.browserSessionId,
    onSuccess: (result) => {
      setRecordedBlocks(result, {
        previous: previous ?? null,
        next: id,
        parent: parentId,
        connectingEdgeType: "default",
      });
    },
  });

  const isProcessing = processRecordingMutation.isPending;

  const isBusy =
    (isProcessing || recordingStore.isRecording) &&
    debugStore.isDebugMode &&
    settingsStore.isUsingABrowser &&
    workflowStatePanel.workflowPanelState.data?.previous === previous &&
    workflowStatePanel.workflowPanelState.data?.next === id &&
    workflowStatePanel.workflowPanelState.data?.parent ===
      (parentId || undefined);

  const isDisabled = !isBusy && recordingStore.isRecording;

  const updateWorkflowPanelState = (
    active: boolean,
    branchContext?: BranchContext,
  ) => {
    setWorkflowPanelState({
      active,
      content: "nodeLibrary",
      data: {
        previous: previous ?? null,
        next: id,
        parent: branchContext?.conditionalNodeId ?? parentId,
        connectingEdgeType: "default",
        branchContext,
      },
    });
  };

  const onAdd = () => {
    if (isDisabled) {
      return;
    }
    const branchContext = deriveBranchContext(previous);
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
    <div
      className={cn("rounded-full bg-slate-50 p-2", {
        "cursor-not-allowed bg-[grey]": isDisabled,
      })}
      onClick={() => {
        onAdd();
      }}
    >
      <PlusIcon className="h-12 w-12 text-slate-950" />
    </div>
  );

  const busy = (
    <WorkflowAdderBusy
      color={isProcessing ? "gray" : "red"}
      operation={isProcessing ? "processing" : "recording"}
      onComplete={() => {
        onEndRecord();
      }}
    >
      {adder}
    </WorkflowAdderBusy>
  );

  const menu = (
    <WorkflowAddMenu onAdd={onAdd} onRecord={onRecord}>
      {adder}
    </WorkflowAddMenu>
  );

  return (
    <div>
      <Handle
        type="source"
        position={Position.Bottom}
        id="a"
        className="opacity-0"
      />
      <Handle
        type="target"
        position={Position.Top}
        id="b"
        className="opacity-0"
      />
      {isBusy ? busy : isDisabled ? adder : menu}
    </div>
  );
}

export { NodeAdderNode };
