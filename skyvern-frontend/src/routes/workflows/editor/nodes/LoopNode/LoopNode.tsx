import { DotsHorizontalIcon, UpdateIcon } from "@radix-ui/react-icons";
import { Handle, NodeProps, Position, useNodes } from "@xyflow/react";
import type { LoopNode } from "./types";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import type { Node } from "@xyflow/react";

function LoopNode({ id, data }: NodeProps<LoopNode>) {
  const nodes = useNodes();
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
                  <span className="text-base">{data.label}</span>
                  <span className="text-xs text-slate-400">Loop Block</span>
                </div>
              </div>
              <div>
                <DotsHorizontalIcon className="h-6 w-6" />
              </div>
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-slate-300">Loop Value</Label>
              <Input
                value={data.loopValue}
                onChange={() => {
                  if (!data.editable) {
                    return;
                  }
                  // TODO
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
