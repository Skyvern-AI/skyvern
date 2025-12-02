import { PlusIcon } from "@radix-ui/react-icons";
import {
  BaseEdge,
  EdgeLabelRenderer,
  EdgeProps,
  getBezierPath,
  useNodes,
} from "@xyflow/react";

import { Button } from "@/components/ui/button";
import { useProcessRecordingMutation } from "@/routes/browserSessions/hooks/useProcessRecordingMutation";
import { useDebugStore } from "@/store/useDebugStore";
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useSettingsStore } from "@/store/SettingsStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { REACT_FLOW_EDGE_Z_INDEX } from "../constants";
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
    onSuccess: (blocks) => {
      setRecordedBlocks(blocks, {
        previous: source,
        next: target,
        parent: sourceNode?.parentId,
        connectingEdgeType: "edgeWithAddButton",
      });
    },
  });

  const isProcessing = processRecordingMutation.isPending;

  const sourceNode = nodes.find((node) => node.id === source);

  const updateWorkflowPanelState = (active: boolean) => {
    setWorkflowPanelState({
      active,
      content: "nodeLibrary",
      data: {
        previous: source,
        next: target,
        parent: sourceNode?.parentId,
      },
    });
  };

  const onAdd = () => updateWorkflowPanelState(true);

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
      className="h-4 w-4 rounded-full transition-all hover:scale-150"
      onClick={() => onAdd()}
    >
      <PlusIcon />
    </Button>
  );

  const menu = (
    <WorkflowAddMenu
      buttonSize="25px"
      gap={35}
      radius="50px"
      startAt={72.5}
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

  const isBusy =
    (isProcessing || recordingStore.isRecording) &&
    debugStore.isDebugMode &&
    settingsStore.isUsingABrowser &&
    workflowStatePanel.workflowPanelState.data?.previous === source &&
    workflowStatePanel.workflowPanelState.data?.next === target &&
    workflowStatePanel.workflowPanelState.data?.parent ===
      (sourceNode?.parentId || undefined);

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
          {isBusy ? busy : menu}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

export { EdgeWithAddButton };
