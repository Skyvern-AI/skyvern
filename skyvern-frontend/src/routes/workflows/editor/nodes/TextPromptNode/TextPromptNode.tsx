import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
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
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { type TextPromptNode } from "./types";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";
import { dataSchemaExampleValue } from "../types";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { ModelSelector } from "@/components/ModelSelector";
import { useDebugStore } from "@/store/useDebugStore";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";

function TextPromptNode({ id, data }: NodeProps<TextPromptNode>) {
  const { updateNodeData } = useReactFlow();
  const { debuggable, editable, label } = data;
  const debugStore = useDebugStore();
  const elideFromDebugging = debugStore.isDebugMode && !debuggable;
  const { blockLabel: urlBlockLabel } = useParams();
  const thisBlockIsPlaying =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const [inputs, setInputs] = useState({
    prompt: data.prompt,
    jsonSchema: data.jsonSchema,
    model: data.model,
  });

  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(nodes, edges, id);

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
      <div
        className={cn(
          "transform-origin-center w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4 transition-all",
          {
            "pointer-events-none bg-slate-950 outline outline-2 outline-slate-300":
              thisBlockIsPlaying,
          },
        )}
      >
        <NodeHeader
          blockLabel={label}
          disabled={elideFromDebugging}
          editable={editable}
          nodeId={id}
          totpIdentifier={null}
          totpUrl={null}
          type="text_prompt" // sic: the naming is not consistent
        />
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
        <ModelSelector
          className="nopan w-52 text-xs"
          value={inputs.model}
          onChange={(value) => {
            handleChange("model", value);
          }}
        />
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
