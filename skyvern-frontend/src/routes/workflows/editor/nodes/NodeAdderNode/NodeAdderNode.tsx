import { SquareIcon, PlusIcon } from "@radix-ui/react-icons";
import { Handle, NodeProps, Position, useEdges } from "@xyflow/react";

import { RadialMenu } from "@/components/RadialMenu";
import { useIsSkyvernUser } from "@/hooks/useIsSkyvernUser";
import { useDebugStore } from "@/store/useDebugStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import type { NodeAdderNode } from "./types";

function NodeAdderNode({ id, parentId }: NodeProps<NodeAdderNode>) {
  const debugStore = useDebugStore();
  const isSkyvernUser = useIsSkyvernUser();
  const edges = useEdges();
  const setWorkflowPanelState = useWorkflowPanelStore(
    (state) => state.setWorkflowPanelState,
  );

  const onAdd = () => {
    const previous = edges.find((edge) => edge.target === id)?.source;
    setWorkflowPanelState({
      active: true,
      content: "nodeLibrary",
      data: {
        previous: previous ?? null,
        next: id,
        parent: parentId,
        connectingEdgeType: "default",
      },
    });
  };

  const adder = (
    <div
      className="rounded-full bg-slate-50 p-2"
      onClick={() => {
        onAdd();
      }}
    >
      <PlusIcon className="h-12 w-12 text-slate-950" />
    </div>
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
      {isSkyvernUser && debugStore.isDebugMode ? (
        <RadialMenu
          items={[
            {
              id: "1",
              icon: <PlusIcon />,
              text: "Add Block",
              onClick: () => {
                onAdd();
              },
            },
            {
              id: "2",
              icon: <SquareIcon />,
              enabled: false,
              text: "Record Browser",
              onClick: () => {
                console.log("Record");
              },
            },
          ]}
          radius="80px"
          startAt={90}
          rotateText={true}
        >
          {adder}
        </RadialMenu>
      ) : (
        adder
      )}
    </div>
  );
}

export { NodeAdderNode };
