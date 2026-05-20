import { PlusIcon } from "@radix-ui/react-icons";
import {
  BaseEdge,
  EdgeLabelRenderer,
  EdgeProps,
  getBezierPath,
  useNodes,
} from "@xyflow/react";
import { useRef } from "react";
import { useParams } from "react-router-dom";

import { Button } from "@/components/ui/button";
import {
  BranchContext,
  useWorkflowPanelStore,
} from "@/store/WorkflowPanelStore";
import { useProcessRecordingMutation } from "@/routes/browserSessions/hooks/useProcessRecordingMutation";
import { useSopToBlocksMutation } from "@/routes/workflows/hooks/useSopToBlocksMutation";
import { useDebugStore } from "@/store/useDebugStore";
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useSettingsStore } from "@/store/SettingsStore";
import { cn } from "@/util/utils";
import { toast } from "@/components/ui/use-toast";

import { REACT_FLOW_EDGE_Z_INDEX } from "../constants";
import { WorkflowAddMenu } from "../WorkflowAddMenu";
import { WorkflowAdderBusy } from "../WorkflowAdderBusy";
import { findBranchContextForInsertion } from "../workflowInsertion";

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
  const { workflowPermanentId } = useParams();
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
  // SOP upload
  const fileInputRef = useRef<HTMLInputElement>(null);

  const sourceNode = nodes.find((node) => node.id === source);

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

  const sopToBlocksMutation = useSopToBlocksMutation({
    onSuccess: (result) => {
      // Reuse existing block insertion pattern
      setRecordedBlocks(result, {
        previous: source,
        next: target,
        parent: sourceNode?.parentId,
        connectingEdgeType: "edgeWithAddButton",
      });
    },
  });

  // Derive upload state directly from mutation to avoid race conditions
  const isUploadingSOP = sopToBlocksMutation.isPending;

  const isProcessing = processRecordingMutation.isPending;

  const isBusy =
    (isProcessing || recordingStore.isRecording) &&
    debugStore.isDebugMode &&
    settingsStore.isUsingABrowser &&
    workflowStatePanel.workflowPanelState.data?.previous === source &&
    workflowStatePanel.workflowPanelState.data?.next === target &&
    workflowStatePanel.workflowPanelState.data?.parent ===
      (sourceNode?.parentId || undefined);

  const isDisabled = !isBusy && recordingStore.isRecording;

  const deriveBranchContext = (): BranchContext | undefined =>
    findBranchContextForInsertion(nodes, source, sourceNode?.parentId);

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
        parent: sourceNode?.parentId,
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
      recordingStore.setIsRecording(true, {
        workflowPermanentId: workflowPermanentId ?? null,
        browserSessionId: settingsStore.browserSessionId,
      });
      updateWorkflowPanelState(false);
    }
  };

  const onEndRecord = () => {
    if (recordingStore.isRecording) {
      recordingStore.setIsRecording(false);
    }

    processRecordingMutation.mutate();
  };

  const onUploadSOP = () => {
    fileInputRef.current?.click();
  };

  const handleSOPFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) {
      return;
    }
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      toast({
        variant: "destructive",
        title: "Invalid file type",
        description: "Please select a PDF file",
      });
      e.target.value = "";
      return;
    }
    sopToBlocksMutation.mutate(file);
    e.target.value = "";
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
      onUploadSOP={onUploadSOP}
      isUploadingSOP={isUploadingSOP}
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

  const handleCancelUpload = () => {
    sopToBlocksMutation.cancel();
  };

  const sopUploadBusy = (
    <WorkflowAdderBusy
      color="#3b82f6"
      operation="uploading"
      size="small"
      onComplete={() => {}}
      onCancel={handleCancelUpload}
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
            transition: "transform 0.2s ease-out",
            fontSize: 12,
            // everything inside EdgeLabelRenderer has no pointer events by default
            // if you have an interactive element, set pointer-events: all
            pointerEvents: "all",
            zIndex: REACT_FLOW_EDGE_Z_INDEX + 1, // above the edge
          }}
          className="nodrag nopan"
        >
          <input
            ref={fileInputRef}
            type="file"
            accept=".pdf"
            className="hidden"
            onChange={handleSOPFileChange}
          />
          {isUploadingSOP
            ? sopUploadBusy
            : isBusy
              ? busy
              : isDisabled
                ? adder
                : menu}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

export { EdgeWithAddButton };
