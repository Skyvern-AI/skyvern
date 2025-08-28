import { useEffect } from "react";
import { Flippable } from "@/components/Flippable";
import { HelpTooltip } from "@/components/HelpTooltip";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import {
  Handle,
  NodeProps,
  Position,
  useEdges,
  useNodes,
  useReactFlow,
} from "@xyflow/react";
import { useState } from "react";
import { dataSchemaExampleValue } from "../types";
import type { ExtractionNode } from "./types";

import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { BlockCodeEditor } from "@/routes/workflows/components/BlockCodeEditor";
import { helpTooltips, placeholders } from "../../helpContent";
import { AppNode } from "..";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { RunEngineSelector } from "@/components/EngineSelector";
import { ModelSelector } from "@/components/ModelSelector";
import { useBlockScriptStore } from "@/store/BlockScriptStore";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useRerender } from "@/hooks/useRerender";

function ExtractionNode({ id, data, type }: NodeProps<ExtractionNode>) {
  const { updateNodeData } = useReactFlow();
  const [facing, setFacing] = useState<"front" | "back">("front");
  const blockScriptStore = useBlockScriptStore();
  const { editable, label } = data;
  const script = blockScriptStore.scripts[label];
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;
  const [inputs, setInputs] = useState({
    url: data.url,
    dataExtractionGoal: data.dataExtractionGoal,
    dataSchema: data.dataSchema,
    maxStepsOverride: data.maxStepsOverride,
    continueOnFailure: data.continueOnFailure,
    cacheActions: data.cacheActions,
    engine: data.engine,
    model: data.model,
  });
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(nodes, edges, id);
  const rerender = useRerender({ prefix: "accordian" });

  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });

  function handleChange(key: string, value: unknown) {
    if (!editable) {
      return;
    }
    setInputs({ ...inputs, [key]: value });
    updateNodeData(id, { [key]: value });
  }

  useEffect(() => {
    setFacing(data.showCode ? "back" : "front");
  }, [data.showCode]);

  return (
    <Flippable facing={facing} preserveFrontsideHeight={true}>
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
              "pointer-events-none": thisBlockIsPlaying,
              "bg-slate-950 outline outline-2 outline-slate-300":
                thisBlockIsTargetted,
            },
          )}
        >
          <NodeHeader
            blockLabel={label}
            editable={editable}
            nodeId={id}
            totpIdentifier={null}
            totpUrl={null}
            type={type}
          />
          <div className="space-y-2">
            <div className="flex justify-between">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">
                  Data Extraction Goal
                </Label>
                <HelpTooltip
                  content={helpTooltips["extraction"]["dataExtractionGoal"]}
                />
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
                if (!editable) {
                  return;
                }
                handleChange("dataExtractionGoal", value);
              }}
              value={inputs.dataExtractionGoal}
              placeholder={placeholders["extraction"]["dataExtractionGoal"]}
              className="nopan text-xs"
            />
          </div>
          <WorkflowDataSchemaInputGroup
            value={inputs.dataSchema}
            onChange={(value) => {
              handleChange("dataSchema", value);
            }}
            exampleValue={dataSchemaExampleValue}
            suggestionContext={{
              data_extraction_goal: inputs.dataExtractionGoal,
              current_schema: inputs.dataSchema,
            }}
          />
          <Separator />
          <Accordion
            type="single"
            collapsible
            onValueChange={() => rerender.bump()}
          >
            <AccordionItem value="advanced" className="border-b-0">
              <AccordionTrigger className="py-0">
                Advanced Settings
              </AccordionTrigger>
              <AccordionContent className="pl-6 pr-1 pt-1">
                <div key={rerender.key} className="space-y-4">
                  <div className="space-y-2">
                    <ModelSelector
                      className="nopan w-52 text-xs"
                      value={inputs.model}
                      onChange={(value) => {
                        handleChange("model", value);
                      }}
                    />
                    <ParametersMultiSelect
                      availableOutputParameters={outputParameterKeys}
                      parameters={data.parameterKeys}
                      onParametersChange={(parameterKeys) => {
                        updateNodeData(id, { parameterKeys });
                      }}
                    />
                  </div>
                  <div className="flex items-center justify-between">
                    <div className="flex gap-2">
                      <Label className="text-xs font-normal text-slate-300">
                        Engine
                      </Label>
                    </div>
                    <RunEngineSelector
                      value={inputs.engine}
                      onChange={(value) => {
                        handleChange("engine", value);
                      }}
                      className="nopan w-52 text-xs"
                    />
                  </div>
                  <div className="flex items-center justify-between">
                    <div className="flex gap-2">
                      <Label className="text-xs font-normal text-slate-300">
                        Max Steps Override
                      </Label>
                      <HelpTooltip
                        content={helpTooltips["extraction"]["maxStepsOverride"]}
                      />
                    </div>
                    <Input
                      type="number"
                      placeholder={
                        placeholders["extraction"]["maxStepsOverride"]
                      }
                      className="nopan w-52 text-xs"
                      min="0"
                      value={inputs.maxStepsOverride ?? ""}
                      onChange={(event) => {
                        if (!editable) {
                          return;
                        }
                        const value =
                          event.target.value === ""
                            ? null
                            : Number(event.target.value);
                        handleChange("maxStepsOverride", value);
                      }}
                    />
                  </div>
                  <Separator />
                  <div className="flex items-center justify-between">
                    <div className="flex gap-2">
                      <Label className="text-xs font-normal text-slate-300">
                        Continue on Failure
                      </Label>
                      <HelpTooltip
                        content={
                          helpTooltips["extraction"]["continueOnFailure"]
                        }
                      />
                    </div>
                    <div className="w-52">
                      <Switch
                        checked={inputs.continueOnFailure}
                        onCheckedChange={(checked) => {
                          if (!editable) {
                            return;
                          }
                          handleChange("continueOnFailure", checked);
                        }}
                      />
                    </div>
                  </div>
                  <div className="flex items-center justify-between">
                    <div className="flex gap-2">
                      <Label className="text-xs font-normal text-slate-300">
                        Cache Actions
                      </Label>
                      <HelpTooltip
                        content={helpTooltips["extraction"]["cacheActions"]}
                      />
                    </div>
                    <div className="w-52">
                      <Switch
                        checked={inputs.cacheActions}
                        onCheckedChange={(checked) => {
                          if (!editable) {
                            return;
                          }
                          handleChange("cacheActions", checked);
                        }}
                      />
                    </div>
                  </div>
                </div>
              </AccordionContent>
            </AccordionItem>
          </Accordion>
        </div>
      </div>
      <BlockCodeEditor blockLabel={label} blockType={type} script={script} />
    </Flippable>
  );
}

export { ExtractionNode };
