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

import {
  BranchContext,
  useWorkflowPanelStore,
} from "@/store/WorkflowPanelStore";
import { useWorkflowScopeReadOnly } from "../WorkflowScopeContext";
import { useProcessRecordingMutation } from "@/routes/browserSessions/hooks/useProcessRecordingMutation";
import { useSopToBlocksMutation } from "@/routes/workflows/hooks/useSopToBlocksMutation";
import { useDebugStore } from "@/store/useDebugStore";
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useSettingsStore } from "@/store/SettingsStore";
import { cn } from "@/util/utils";
import { toast } from "@/components/ui/use-toast";

import {
  REACT_FLOW_EDGE_Z_INDEX,
  REACT_FLOW_SELECTED_NODE_Z,
} from "../constants";
import { SELECTED_RING_CLASSES } from "../selection/selectedRingClasses";
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
  const isReadOnly = useWorkflowScopeReadOnly();
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
    (debugStore.isDebugMode || debugStore.blockRunsEnabled) &&
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

  // Mirror NodeAdderNode's selection treatment so the two + CTAs feel like one affordance.
  const isAdding =
    workflowStatePanel.workflowPanelState.active &&
    workflowStatePanel.workflowPanelState.content === "nodeLibrary" &&
    workflowStatePanel.workflowPanelState.data?.previous === source &&
    workflowStatePanel.workflowPanelState.data?.next === target &&
    workflowStatePanel.workflowPanelState.data?.parent ===
      (sourceNode?.parentId || undefined);

  const adder = (
    <div
      className={cn(
        "flex h-6 w-6 items-center justify-center rounded-full bg-slate-50 transition-colors hover:bg-blue-50 hover:ring-2 hover:ring-blue-500/40",
        {
          "cursor-not-allowed bg-muted text-muted-foreground hover:bg-muted hover:ring-0":
            isDisabled,
          [SELECTED_RING_CLASSES]: isAdding,
        },
      )}
      role="button"
      tabIndex={isDisabled ? -1 : 0}
      aria-label="Add block"
      aria-disabled={isDisabled}
      data-testid="edge-add-button"
      onClick={() => onAdd()}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onAdd();
        }
      }}
    >
      <PlusIcon className="h-4 w-4 text-slate-950" />
    </div>
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

  if (isReadOnly) {
    return <BaseEdge path={edgePath} markerEnd={markerEnd} style={style} />;
  }

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
            // Inside parent nodes (e.g. for-loop), xyflow elevates the edge SVG
            // via getElevatedEdgeZIndex (= edge.zIndex + sourceNode.internals.z).
            // Stay above that elevated stack so the line never paints over the +.
            zIndex: REACT_FLOW_EDGE_Z_INDEX + REACT_FLOW_SELECTED_NODE_Z + 1,
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
