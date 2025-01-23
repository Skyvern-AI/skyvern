import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { WorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
import {
  Handle,
  NodeProps,
  Position,
  useEdges,
  useNodes,
  useReactFlow,
} from "@xyflow/react";
import { useState } from "react";
import { AppNode } from "..";
import { helpTooltips } from "../../helpContent";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { WorkflowBlockIcon } from "../WorkflowBlockIcon";
import { type TextPromptNode } from "./types";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";
import { dataSchemaExampleValue } from "../types";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";

function TextPromptNode({ id, data }: NodeProps<TextPromptNode>) {
  const { updateNodeData } = useReactFlow();
  const { editable } = data;
  const deleteNodeCallback = useDeleteNodeCallback();
  const [inputs, setInputs] = useState({
    prompt: data.prompt,
    jsonSchema: data.jsonSchema,
  });

  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(nodes, edges, id);

  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });

  function handleChange(key: string, value: unknown) {
    if (!editable) {
      return;
    }
    setInputs({ ...inputs, [key]: value });
    updateNodeData(id, { [key]: value });
  }

  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });

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
                workflowBlockType={WorkflowBlockTypes.TextPrompt}
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
              <span className="text-xs text-slate-400">Text Prompt Block</span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </div>
        <div className="space-y-2">
          <div className="flex justify-between">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">Prompt</Label>
              <HelpTooltip content={helpTooltips["textPrompt"]["prompt"]} />
            </div>
            {isFirstWorkflowBlock ? (
              <div className="flex justify-end text-xs text-slate-400">
                Tip: Use the {"+"} button to add parameters!
              </div>
            ) : null}
          </div>

          <WorkflowBlockInputTextarea
            nodeId={id}
            onChange={(value) => {
              handleChange("prompt", value);
            }}
            value={inputs.prompt}
            placeholder="What do you want to generate?"
            className="nopan text-xs"
          />
        </div>
        <div className="space-y-2">
          <ParametersMultiSelect
            availableOutputParameters={outputParameterKeys}
            parameters={data.parameterKeys}
            onParametersChange={(parameterKeys) => {
              updateNodeData(id, { parameterKeys });
            }}
          />
        </div>
        <Separator />
        <WorkflowDataSchemaInputGroup
          exampleValue={dataSchemaExampleValue}
          value={inputs.jsonSchema}
          onChange={(value) => {
            handleChange("jsonSchema", value);
          }}
          suggestionContext={{}}
        />
      </div>
    </div>
  );
}

export { TextPromptNode };
