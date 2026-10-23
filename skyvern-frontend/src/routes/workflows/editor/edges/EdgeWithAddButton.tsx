import { PlusIcon } from "@radix-ui/react-icons";
import {
  BaseEdge,
  EdgeLabelRenderer,
  EdgeProps,
  getBezierPath,
  useNodes,
} from "@xyflow/react";
import { useRef } from "react";

import {
  BranchContext,
  useWorkflowPanelStore,
} from "@/store/WorkflowPanelStore";
import { useWorkflowScopeReadOnly } from "../WorkflowScopeContext";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useRecordingLauncherStore } from "@/store/useRecordingLauncherStore";
import { cn } from "@/util/utils";
import { toast } from "@/components/ui/use-toast";

import {
  REACT_FLOW_EDGE_Z_INDEX,
  REACT_FLOW_SELECTED_NODE_Z,
} from "../constants";
import { SELECTED_RING_CLASSES } from "../selection/selectedRingClasses";
import { WorkflowAddMenu } from "../WorkflowAddMenu";
import { findBranchContextForInsertion } from "../workflowInsertion";
import {
  blockLabelFor,
  demonstrationInsertionFromNodes,
  sopAttachmentDraft,
  stageSopFile,
} from "../workspaceAuthoringActions";

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
  const nodes = useNodes();
  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });
  const startDemonstration = useRecordingLauncherStore(
    (state) => state.startDemonstration,
  );
  const stageAttachment = useCopilotActionStore(
    (state) => state.stageAttachment,
  );
  const workflowStatePanel = useWorkflowPanelStore();
  const setWorkflowPanelState = useWorkflowPanelStore(
    (state) => state.setWorkflowPanelState,
  );
  // SOP upload
  const fileInputRef = useRef<HTMLInputElement>(null);

  const sourceNode = nodes.find((node) => node.id === source);

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
    const branchContext = deriveBranchContext();
    updateWorkflowPanelState(true, branchContext);
  };

  const onRecord = () => {
    const insertion = demonstrationInsertionFromNodes(nodes, {
      previous: source,
      next: target,
      parent: sourceNode?.parentId,
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
          previous: source,
          next: target,
          parent: sourceNode?.parentId,
        }),
        blockLabelFor(nodes, sourceNode?.parentId),
      ),
    );
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
        { [SELECTED_RING_CLASSES]: isAdding },
      )}
      role="button"
      tabIndex={0}
      aria-label="Add block"
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
      canUploadSOP={stageAttachment !== null}
      canRecordTask={startDemonstration !== null}
    >
      {adder}
    </WorkflowAddMenu>
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
          {menu}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

export { EdgeWithAddButton };
