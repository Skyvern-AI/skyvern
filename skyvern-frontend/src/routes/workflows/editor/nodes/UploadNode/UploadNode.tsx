import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { UploadIcon } from "@radix-ui/react-icons";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import type { UploadNode } from "./types";

function UploadNode({ id, data }: NodeProps<UploadNode>) {
  const { updateNodeData } = useReactFlow();
  const deleteNodeCallback = useDeleteNodeCallback();

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
              <UploadIcon className="h-6 w-6" />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={data.label}
                editable={data.editable}
                onChange={(value) => updateNodeData(id, { label: value })}
              />
              <span className="text-xs text-slate-400">Upload Block</span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </div>
        <div className="space-y-4">
          <div className="space-y-1">
            <Label className="text-sm text-slate-400">File Path</Label>
            <Input
              value={data.path}
              onChange={(event) => {
                if (!data.editable) {
                  return;
                }
                updateNodeData(id, { path: event.target.value });
              }}
              className="nopan"
            />
          </div>
        </div>
      </div>
    </div>
  );
}

export { UploadNode };
