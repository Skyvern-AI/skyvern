import { PlusIcon } from "@radix-ui/react-icons";
import { Handle, NodeProps, Position, useEdges, useNodes } from "@xyflow/react";
import { useRef, useState } from "react";

import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import {
  BranchContext,
  useWorkflowPanelStore,
} from "@/store/WorkflowPanelStore";
import { useWorkflowSettingsStore } from "@/store/WorkflowSettingsStore";
import { useRecordingLauncherStore } from "@/store/useRecordingLauncherStore";
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
import { SELECTED_RING_CLASSES } from "../../selection/selectedRingClasses";
import { useWorkflowScopeReadOnly } from "../../WorkflowScopeContext";
import { findBranchContextForInsertion } from "../../workflowInsertion";
import {
  blockLabelFor,
  demonstrationInsertionFromNodes,
  sopAttachmentDraft,
  stageSopFile,
} from "../../workspaceAuthoringActions";

function NodeAdderNode({ id, parentId }: NodeProps<NodeAdderNode>) {
  const edges = useEdges();
  const nodes = useNodes();
  const startDemonstration = useRecordingLauncherStore(
    (state) => state.startDemonstration,
  );
  const stageAttachment = useCopilotActionStore(
    (state) => state.stageAttachment,
  );
  const workflowSettingsStore = useWorkflowSettingsStore();
  const setWorkflowPanelState = useWorkflowPanelStore(
    (state) => state.setWorkflowPanelState,
  );
  const workflowStatePanel = useWorkflowPanelStore();

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

  const isBlockedByFinally =
    !parentId && Boolean(workflowSettingsStore.finallyBlockLabel);
  // Read-only canvases (WorkflowComparisonPanel) must not let the `+`
  // affordance open the node library — selecting a block from there
  // would route through `addNode` and mutate the underlying workflow.
  const isReadOnlyScope = useWorkflowScopeReadOnly();
  const isDisabled = isReadOnlyScope || isBlockedByFinally;
  const disabledReason: string | null = isReadOnlyScope
    ? "This canvas is read-only"
    : isBlockedByFinally
      ? "Finally block must run last - choose a position above it"
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
    const insertion = demonstrationInsertionFromNodes(nodes, {
      previous: previous ?? null,
      next: id,
      parent: parentId,
    });
    if (!insertion) {
      toast({
        title: "Recording inside a loop or branch isn't supported yet",
      });
      return;
    }
    startDemonstration?.({ insertion });
  };

  const onUploadSOP = () => {
    fileInputRef.current?.click();
  };

  const handleSOPFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) {
      return;
    }
    stageSopFile(
      file,
      sopAttachmentDraft(
        demonstrationInsertionFromNodes(nodes, {
          previous: previous ?? null,
          next: id,
          parent: parentId,
        }),
        blockLabelFor(nodes, parentId),
      ),
    );
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

  const menu = (
    <WorkflowAddMenu
      onAdd={onAdd}
      onRecord={onRecord}
      onUploadSOP={onUploadSOP}
      canUploadSOP={stageAttachment !== null}
      canRecordTask={startDemonstration !== null}
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
      {isDisabled ? adder : menu}
    </div>
  );
}

export { NodeAdderNode };
