import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import type { CodeBlockNode } from "./types";
import { Label } from "@/components/ui/label";
import { CodeIcon, DotsHorizontalIcon } from "@radix-ui/react-icons";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { EditableNodeTitle } from "../components/EditableNodeTitle";

function CodeBlockNode({ id, data }: NodeProps<CodeBlockNode>) {
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
              <CodeIcon className="h-6 w-6" />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={data.label}
                editable={data.editable}
                onChange={(value) => updateNodeData(id, { label: value })}
              />
              <span className="text-xs text-slate-400">Code Block</span>
            </div>
          </div>
          <div>
            <DotsHorizontalIcon className="h-6 w-6" />
          </div>
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-slate-300">Code Input</Label>
          <CodeEditor
            language="python"
            value={data.code}
            onChange={(value) => {
              if (!data.editable) {
                return;
              }
              updateNodeData(id, { code: value });
            }}
            className="nopan"
          />
        </div>
      </div>
    </div>
  );
}

export { CodeBlockNode };
