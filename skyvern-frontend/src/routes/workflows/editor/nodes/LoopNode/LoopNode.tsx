import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { UpdateIcon } from "@radix-ui/react-icons";
import type { Node } from "@xyflow/react";
import {
  Handle,
  NodeProps,
  Position,
  useNodes,
  useReactFlow,
} from "@xyflow/react";
import { useState } from "react";
import { AppNode } from "..";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import type { LoopNode } from "./types";

function LoopNode({ id, data }: NodeProps<LoopNode>) {
  const { updateNodeData } = useReactFlow();
  const nodes = useNodes<AppNode>();
  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });
  const deleteNodeCallback = useDeleteNodeCallback();
  const [inputs, setInputs] = useState({
    loopValue: data.loopValue,
  });

  const children = nodes.filter((node) => node.parentId === id);
  const furthestDownChild: Node | null = children.reduce(
    (acc, child) => {
      if (!acc) {
        return child;
      }
      if (child.position.y > acc.position.y) {
        return child;
      }
      return acc;
    },
    null as Node | null,
  );

  const childrenHeightExtent =
    (furthestDownChild?.measured?.height ?? 0) +
    (furthestDownChild?.position.y ?? 0) +
    24;

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
      <div
        className="w-[60rem] rounded-md border-2 border-dashed border-slate-600 p-2"
        style={{
          height: childrenHeightExtent,
        }}
      >
        <div className="flex w-full justify-center">
          <div className="w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4">
            <div className="flex h-[2.75rem] justify-between">
              <div className="flex gap-2">
                <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
                  <UpdateIcon className="h-6 w-6" />
                </div>
                <div className="flex flex-col gap-1">
                  <EditableNodeTitle
                    value={label}
                    editable={data.editable}
                    onChange={setLabel}
                    titleClassName="text-base"
                    inputClassName="text-base"
                  />
                  <span className="text-xs text-slate-400">Loop Block</span>
                </div>
              </div>
              <NodeActionMenu
                onDelete={() => {
                  deleteNodeCallback(id);
                }}
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-slate-300">Loop Value</Label>
              <Input
                value={inputs.loopValue}
                onChange={(event) => {
                  if (!data.editable) {
                    return;
                  }
                  setInputs({ ...inputs, loopValue: event.target.value });
                  updateNodeData(id, { loopValue: event.target.value });
                }}
                placeholder="What value are you iterating over?"
                className="nopan"
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export { LoopNode };
