import { HelpTooltip } from "@/components/HelpTooltip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { WorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { useState } from "react";
import { helpTooltips } from "../../helpContent";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { WorkflowBlockIcon } from "../WorkflowBlockIcon";
import { type PDFParserNode } from "./types";
import { Checkbox } from "@/components/ui/checkbox";
import { dataSchemaExampleForFileExtraction } from "../types";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";

function PDFParserNode({ id, data }: NodeProps<PDFParserNode>) {
  const { updateNodeData } = useReactFlow();
  const deleteNodeCallback = useDeleteNodeCallback();
  const [inputs, setInputs] = useState({
    fileUrl: data.fileUrl,
    dataSchema: data.jsonSchema,
  });
  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });

  function handleChange(key: string, value: unknown) {
    if (!data.editable) {
      return;
    }
    setInputs({ ...inputs, [key]: value });
    updateNodeData(id, { [key]: value });
  }

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
              <WorkflowBlockIcon
                workflowBlockType={WorkflowBlockTypes.PDFParser}
                className="size-6"
              />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={label}
                editable={data.editable}
                onChange={setLabel}
                titleClassName="text-base"
                inputClassName="text-base"
              />
              <span className="text-xs text-slate-400">PDF Parser Block</span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </div>
        <div className="space-y-4">
          <div className="space-y-2">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">File URL</Label>
              <HelpTooltip content={helpTooltips["fileParser"]["fileUrl"]} />
            </div>
            <Input
              value={inputs.fileUrl}
              onChange={(event) => {
                if (!data.editable) {
                  return;
                }
                setInputs({ ...inputs, fileUrl: event.target.value });
                updateNodeData(id, { fileUrl: event.target.value });
              }}
              className="nopan text-xs"
            />
          </div>
          <div className="space-y-2">
            <div className="flex gap-4">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">Data Schema</Label>
                <HelpTooltip content={helpTooltips["task"]["dataSchema"]} />
              </div>
              <Checkbox
                checked={inputs.dataSchema !== "null"}
                onCheckedChange={(checked) => {
                  handleChange(
                    "dataSchema",
                    checked
                      ? JSON.stringify(
                          dataSchemaExampleForFileExtraction,
                          null,
                          2,
                        )
                      : "null",
                  );
                }}
              />
            </div>
            {inputs.dataSchema !== "null" && (
              <div>
                <CodeEditor
                  language="json"
                  value={inputs.dataSchema}
                  onChange={(value) => {
                    handleChange("dataSchema", value);
                  }}
                  className="nowheel nopan"
                  fontSize={8}
                />
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export { PDFParserNode };
