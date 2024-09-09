import { CursorTextIcon, DotsHorizontalIcon } from "@radix-ui/react-icons";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import type { TextPromptNode } from "./types";
import { Label } from "@/components/ui/label";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Separator } from "@/components/ui/separator";
import { Checkbox } from "@/components/ui/checkbox";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { EditableNodeTitle } from "../components/EditableNodeTitle";

function TextPromptNode({ id, data }: NodeProps<TextPromptNode>) {
  const { updateNodeData } = useReactFlow();
  const { editable } = data;

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
                editable={editable}
                onChange={(value) => updateNodeData(id, { label: value })}
              />
              <span className="text-xs text-slate-400">Text Prompt Block</span>
            </div>
          </div>
          <div>
            <DotsHorizontalIcon className="h-6 w-6" />
          </div>
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-slate-300">Prompt</Label>
          <AutoResizingTextarea
            onChange={(event) => {
              if (!editable) {
                return;
              }
              updateNodeData(id, { prompt: event.target.value });
            }}
            value={data.prompt}
            placeholder="What do you want to generate?"
            className="nopan"
          />
        </div>
        <Separator />
        <div className="space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">Data Schema</Label>
            <Checkbox
              checked={data.jsonSchema !== "null"}
              onCheckedChange={(checked) => {
                if (!editable) {
                  return;
                }
                updateNodeData(id, {
                  jsonSchema: checked ? "{}" : "null",
                });
              }}
            />
          </div>
          {data.jsonSchema !== "null" && (
            <div>
              <CodeEditor
                language="json"
                value={data.jsonSchema}
                onChange={(value) => {
                  if (!editable) {
                    return;
                  }
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
