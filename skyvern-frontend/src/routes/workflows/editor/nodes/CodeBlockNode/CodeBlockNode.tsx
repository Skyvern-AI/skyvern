import { Label } from "@/components/ui/label";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { CodeIcon } from "@radix-ui/react-icons";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { useState } from "react";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import type { CodeBlockNode } from "./types";

function CodeBlockNode({ id, data }: NodeProps<CodeBlockNode>) {
  const { updateNodeData } = useReactFlow();
  const deleteNodeCallback = useDeleteNodeCallback();
  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });
  const [inputs, setInputs] = useState({
    code: data.code,
  });

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
                value={label}
                editable={data.editable}
                onChange={setLabel}
                titleClassName="text-base"
                inputClassName="text-base"
              />
              <span className="text-xs text-slate-400">Code Block</span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </div>
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Code Input</Label>
          <CodeEditor
            language="python"
            value={inputs.code}
            onChange={(value) => {
              if (!data.editable) {
                return;
              }
              setInputs({ ...inputs, code: value });
              updateNodeData(id, { code: value });
            }}
            className="nopan"
            fontSize={8}
          />
        </div>
      </div>
    </div>
  );
}

export { CodeBlockNode };
