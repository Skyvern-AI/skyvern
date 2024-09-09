import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import type { FileParserNode } from "./types";
import { CursorTextIcon, DotsHorizontalIcon } from "@radix-ui/react-icons";
import { Input } from "@/components/ui/input";
import { EditableNodeTitle } from "../components/EditableNodeTitle";

function FileParserNode({ id, data }: NodeProps<FileParserNode>) {
  const { updateNodeData } = useReactFlow();
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
      <div className="w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4">
        <div className="flex h-[2.75rem] justify-between">
          <div className="flex gap-2">
            <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
              <CursorTextIcon className="h-6 w-6" />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={data.label}
                editable={data.editable}
                onChange={(value) => updateNodeData(id, { label: value })}
              />
              <span className="text-xs text-slate-400">File Parser Block</span>
            </div>
          </div>
          <div>
            <DotsHorizontalIcon className="h-6 w-6" />
          </div>
        </div>
        <div className="space-y-4">
          <div className="space-y-1">
            <span className="text-sm text-slate-400">File URL</span>
            <Input
              value={data.fileUrl}
              onChange={(event) => {
                if (!data.editable) {
                  return;
                }
                updateNodeData(id, { fileUrl: event.target.value });
              }}
              className="nopan"
            />
          </div>
        </div>
      </div>
    </div>
  );
}

export { FileParserNode };
