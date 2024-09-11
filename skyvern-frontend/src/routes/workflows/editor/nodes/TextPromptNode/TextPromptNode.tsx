import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { CursorTextIcon } from "@radix-ui/react-icons";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import type { TextPromptNode } from "./types";
import { useState } from "react";

function TextPromptNode({ id, data }: NodeProps<TextPromptNode>) {
  const { updateNodeData } = useReactFlow();
  const { editable } = data;
  const deleteNodeCallback = useDeleteNodeCallback();
  const [label, setLabel] = useState(data.label);
  const [inputs, setInputs] = useState({
    prompt: data.prompt,
    jsonSchema: data.jsonSchema,
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
              <CursorTextIcon className="h-6 w-6" />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={label}
                editable={editable}
                onChange={(value) => {
                  setLabel(value);
                  updateNodeData(id, { label: value });
                }}
              />
              <span className="text-xs text-slate-400">Text Prompt Block</span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-slate-300">Prompt</Label>
          <AutoResizingTextarea
            onChange={(event) => {
              if (!editable) {
                return;
              }
              setInputs({ ...inputs, prompt: event.target.value });
              updateNodeData(id, { prompt: event.target.value });
            }}
            value={inputs.prompt}
            placeholder="What do you want to generate?"
            className="nopan"
          />
        </div>
        <Separator />
        <div className="space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">Data Schema</Label>
            <Checkbox
              checked={inputs.jsonSchema !== "null"}
              onCheckedChange={(checked) => {
                if (!editable) {
                  return;
                }
                setInputs({
                  ...inputs,
                  jsonSchema: checked ? "{}" : "null",
                });
                updateNodeData(id, {
                  jsonSchema: checked ? "{}" : "null",
                });
              }}
            />
          </div>
          {inputs.jsonSchema !== "null" && (
            <div>
              <CodeEditor
                language="json"
                value={inputs.jsonSchema}
                onChange={(value) => {
                  if (!editable) {
                    return;
                  }
                  setInputs({ ...inputs, jsonSchema: value });
                  updateNodeData(id, { jsonSchema: value });
                }}
                className="nowheel nopan"
              />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export { TextPromptNode };
