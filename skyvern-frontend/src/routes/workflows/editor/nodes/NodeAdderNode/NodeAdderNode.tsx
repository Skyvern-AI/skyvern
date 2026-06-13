import { PlusIcon } from "@radix-ui/react-icons";
import { Handle, NodeProps, Position, useEdges, useNodes } from "@xyflow/react";
import { useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { useProcessRecordingMutation } from "@/routes/browserSessions/hooks/useProcessRecordingMutation";
import { useSopToBlocksMutation } from "@/routes/workflows/hooks/useSopToBlocksMutation";
import { useDebugStore } from "@/store/useDebugStore";
import {
  BranchContext,
  useWorkflowPanelStore,
} from "@/store/WorkflowPanelStore";
import { useWorkflowSettingsStore } from "@/store/WorkflowSettingsStore";
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useSettingsStore } from "@/store/SettingsStore";
import { cn } from "@/util/utils";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { toast } from "@/components/ui/use-toast";

import type { NodeAdderNode } from "./types";
import { WorkflowAddMenu } from "../../WorkflowAddMenu";
import { WorkflowAdderBusy } from "../../WorkflowAdderBusy";
import { SELECTED_RING_CLASSES } from "../../selection/selectedRingClasses";
import { useWorkflowScopeReadOnly } from "../../WorkflowScopeContext";
import { findBranchContextForInsertion } from "../../workflowInsertion";

function NodeAdderNode({ id, parentId }: NodeProps<NodeAdderNode>) {
  const { workflowPermanentId } = useParams();
  const edges = useEdges();
  const nodes = useNodes();
  const debugStore = useDebugStore();
  const recordingStore = useRecordingStore();
  const settingsStore = useSettingsStore();
  const workflowSettingsStore = useWorkflowSettingsStore();
  const setWorkflowPanelState = useWorkflowPanelStore(
    (state) => state.setWorkflowPanelState,
  );
  const workflowStatePanel = useWorkflowPanelStore();
  const setRecordedBlocks = useRecordedBlocksStore(
    (state) => state.setRecordedBlocks,
  );

  // SOP upload
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [menuPinned, setMenuPinned] = useState(false);

  const deriveBranchContext = (previousNodeId: string | undefined) =>
    findBranchContextForInsertion(nodes, previousNodeId, parentId);

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

  const sopToBlocksMutation = useSopToBlocksMutation({
    onSuccess: (result) => {
      // Reuse existing block insertion pattern
      setRecordedBlocks(result, {
        previous: previous ?? null,
        next: id,
        parent: parentId,
        connectingEdgeType: "default",
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
    workflowStatePanel.workflowPanelState.data?.previous === previous &&
    workflowStatePanel.workflowPanelState.data?.next === id &&
    workflowStatePanel.workflowPanelState.data?.parent ===
      (parentId || undefined);

  const isBlockedByFinally =
    !parentId && Boolean(workflowSettingsStore.finallyBlockLabel);
  // Read-only canvases (WorkflowComparisonPanel) must not let the `+`
  // affordance open the node library — selecting a block from there
  // would route through `addNode` and mutate the underlying workflow.
  const isReadOnlyScope = useWorkflowScopeReadOnly();
  const isDisabled =
    isReadOnlyScope ||
    isBlockedByFinally ||
    (!isBusy && recordingStore.isRecording);
  const disabledReason: string | null = isReadOnlyScope
    ? "This canvas is read-only"
    : isBlockedByFinally
      ? "Finally block must run last - choose a position above it"
      : !isBusy && recordingStore.isRecording
        ? "Stop recording to add a block"
        : null;

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
        parent: parentId,
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

  // Highlight the + CTA the same way withSelectableBlock highlights a selected
  // block, while its add menu is pinned open or the node library is open targeting it.
  const isAdding =
    workflowStatePanel.workflowPanelState.active &&
    workflowStatePanel.workflowPanelState.content === "nodeLibrary" &&
    workflowStatePanel.workflowPanelState.data?.previous === previous &&
    workflowStatePanel.workflowPanelState.data?.next === id &&
    workflowStatePanel.workflowPanelState.data?.parent ===
      (parentId || undefined);

  const adderInner = (
    <div
      data-testid="node-adder-button"
      className={cn(
        "rounded-full bg-slate-50 p-1 transition-colors hover:bg-blue-50 hover:ring-2 hover:ring-blue-500/40",
        {
          "cursor-not-allowed bg-muted text-muted-foreground hover:bg-muted hover:ring-0":
            isDisabled,
          [SELECTED_RING_CLASSES]: isAdding || menuPinned,
        },
      )}
      onClick={() => {
        onAdd();
      }}
    >
      <PlusIcon className="h-8 w-8 text-slate-950" />
    </div>
  );
  const adder =
    isDisabled && disabledReason ? (
      <TooltipProvider delayDuration={150}>
        <Tooltip>
          <TooltipTrigger asChild>
            <div>{adderInner}</div>
          </TooltipTrigger>
          <TooltipContent>{disabledReason}</TooltipContent>
        </Tooltip>
      </TooltipProvider>
    ) : (
      adderInner
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

  const handleCancelUpload = () => {
    sopToBlocksMutation.cancel();
  };

  const sopUploadBusy = (
    <WorkflowAdderBusy
      color="#3b82f6"
      operation="uploading"
      onComplete={() => {}}
      onCancel={handleCancelUpload}
    >
      {adder}
    </WorkflowAdderBusy>
  );

  const menu = (
    <WorkflowAddMenu
      onAdd={onAdd}
      onRecord={onRecord}
      onUploadSOP={onUploadSOP}
      isUploadingSOP={isUploadingSOP}
      onPinnedChange={setMenuPinned}
    >
      {adder}
    </WorkflowAddMenu>
  );

  return (
    <div data-tour="node-adder">
      <input
        ref={fileInputRef}
        type="file"
        accept=".pdf"
        className="hidden"
        onChange={handleSOPFileChange}
      />
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
      {isUploadingSOP
        ? sopUploadBusy
        : isBusy
          ? busy
          : isDisabled
            ? adder
            : menu}
    </div>
  );
}

export { NodeAdderNode };
