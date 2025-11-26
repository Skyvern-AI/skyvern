import { SquareIcon, PlusIcon } from "@radix-ui/react-icons";
import {
  BaseEdge,
  EdgeLabelRenderer,
  EdgeProps,
  getBezierPath,
  useNodes,
} from "@xyflow/react";

import { Button } from "@/components/ui/button";
import { RadialMenu } from "@/components/RadialMenu";
import { useIsSkyvernUser } from "@/hooks/useIsSkyvernUser";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useDebugStore } from "@/store/useDebugStore";

import { REACT_FLOW_EDGE_Z_INDEX } from "../constants";

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
  const debugStore = useDebugStore();
  const isSkyvernUser = useIsSkyvernUser();
  const nodes = useNodes();
  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });
  const setWorkflowPanelState = useWorkflowPanelStore(
    (state) => state.setWorkflowPanelState,
  );
  const sourceNode = nodes.find((node) => node.id === source);

  const onAdd = () => {
    setWorkflowPanelState({
      active: true,
      content: "nodeLibrary",
      data: {
        previous: source,
        next: target,
        parent: sourceNode?.parentId,
      },
    });
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
          {isSkyvernUser && debugStore.isDebugMode ? (
            <RadialMenu
              items={[
                {
                  id: "1",
                  icon: <PlusIcon className="h-3 w-3" />,
                  text: "Add Block",
                  onClick: () => {
                    onAdd();
                  },
                },
                {
                  id: "2",
                  icon: <SquareIcon className="h-3 w-3" />,
                  enabled: false,
                  text: "Record Browser",
                  onClick: () => {
                    console.log("Record");
                  },
                },
              ]}
              buttonSize="25px"
              radius="50px"
              startAt={72.5}
              gap={35}
              rotateText={true}
            >
              {adder}
            </RadialMenu>
          ) : (
            adder
          )}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

export { EdgeWithAddButton };
