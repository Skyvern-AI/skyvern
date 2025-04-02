import { HelpTooltip } from "@/components/HelpTooltip";
import { ExtractIcon } from "@/components/icons/ExtractIcon";
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
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import {
  Handle,
  NodeProps,
  Position,
  useEdges,
  useNodes,
  useReactFlow,
} from "@xyflow/react";
import { useState } from "react";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { dataSchemaExampleValue } from "../types";
import type { ExtractionNode } from "./types";

import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { helpTooltips, placeholders } from "../../helpContent";
import { AppNode } from "..";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";

function ExtractionNode({ id, data }: NodeProps<ExtractionNode>) {
  const { updateNodeData } = useReactFlow();
  const { editable } = data;
  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });
  const [inputs, setInputs] = useState({
    url: data.url,
    dataExtractionGoal: data.dataExtractionGoal,
    dataSchema: data.dataSchema,
    maxStepsOverride: data.maxStepsOverride,
    continueOnFailure: data.continueOnFailure,
    cacheActions: data.cacheActions,
  });
  const deleteNodeCallback = useDeleteNodeCallback();
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(nodes, edges, id);

  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });

  function handleChange(key: string, value: unknown) {
    if (!editable) {
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
        <header className="flex h-[2.75rem] justify-between">
          <div className="flex gap-2">
            <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
              <ExtractIcon className="size-6" />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={label}
                editable={editable}
                onChange={setLabel}
                titleClassName="text-base"
                inputClassName="text-base"
              />
              <span className="text-xs text-slate-400">Extraction Block</span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </header>
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
        <Accordion type="single" collapsible>
          <AccordionItem value="advanced" className="border-b-0">
            <AccordionTrigger className="py-0">
              Advanced Settings
            </AccordionTrigger>
            <AccordionContent className="pl-6 pr-1 pt-1">
              <div className="space-y-4">
                <div className="space-y-2">
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
                      Max Steps Override
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["extraction"]["maxStepsOverride"]}
                    />
                  </div>
                  <Input
                    type="number"
                    placeholder={placeholders["extraction"]["maxStepsOverride"]}
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
                      content={helpTooltips["extraction"]["continueOnFailure"]}
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
  );
}

export { ExtractionNode };
