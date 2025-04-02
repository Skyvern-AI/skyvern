import { HelpTooltip } from "@/components/HelpTooltip";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
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
import { helpTooltips, placeholders } from "../../helpContent";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { dataSchemaExampleValue, errorMappingExampleValue } from "../types";
import { WorkflowBlockIcon } from "../WorkflowBlockIcon";
import { ParametersMultiSelect } from "./ParametersMultiSelect";
import type { TaskNode } from "./types";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";

function TaskNode({ id, data }: NodeProps<TaskNode>) {
  const { updateNodeData } = useReactFlow();
  const { editable } = data;
  const deleteNodeCallback = useDeleteNodeCallback();
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(nodes, edges, id);
  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });

  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });

  const [inputs, setInputs] = useState({
    url: data.url,
    navigationGoal: data.navigationGoal,
    dataExtractionGoal: data.dataExtractionGoal,
    completeCriterion: data.completeCriterion,
    terminateCriterion: data.terminateCriterion,
    dataSchema: data.dataSchema,
    maxStepsOverride: data.maxStepsOverride,
    allowDownloads: data.allowDownloads,
    continueOnFailure: data.continueOnFailure,
    cacheActions: data.cacheActions,
    downloadSuffix: data.downloadSuffix,
    errorCodeMapping: data.errorCodeMapping,
    totpVerificationUrl: data.totpVerificationUrl,
    totpIdentifier: data.totpIdentifier,
  });

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
      <div className="w-[30rem] space-y-2 rounded-lg bg-slate-elevation3 px-6 py-4">
        <div className="flex h-[2.75rem] justify-between">
          <div className="flex gap-2">
            <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
              <WorkflowBlockIcon
                workflowBlockType={WorkflowBlockTypes.Task}
                className="size-6"
              />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={label}
                editable={editable}
                onChange={setLabel}
                titleClassName="text-base"
                inputClassName="text-base"
              />
              <span className="text-xs text-slate-400">Task Block</span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </div>
        <Accordion type="multiple" defaultValue={["content", "extraction"]}>
          <AccordionItem value="content">
            <AccordionTrigger>Content</AccordionTrigger>
            <AccordionContent className="pl-[1.5rem] pr-1">
              <div className="space-y-4">
                <div className="space-y-2">
                  <div className="flex justify-between">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">URL</Label>
                      <HelpTooltip content={helpTooltips["task"]["url"]} />
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
                      handleChange("url", value);
                    }}
                    value={inputs.url}
                    placeholder={placeholders["task"]["url"]}
                    className="nopan text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">Goal</Label>
                    <HelpTooltip
                      content={helpTooltips["task"]["navigationGoal"]}
                    />
                  </div>
                  <WorkflowBlockInputTextarea
                    nodeId={id}
                    onChange={(value) => {
                      handleChange("navigationGoal", value);
                    }}
                    value={inputs.navigationGoal}
                    placeholder={placeholders["task"]["navigationGoal"]}
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
              </div>
            </AccordionContent>
          </AccordionItem>
          <AccordionItem value="extraction">
            <AccordionTrigger>Extraction</AccordionTrigger>
            <AccordionContent className="pl-[1.5rem] pr-1">
              <div className="space-y-4">
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">
                      Data Extraction Goal
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["task"]["dataExtractionGoal"]}
                    />
                  </div>
                  <WorkflowBlockInputTextarea
                    nodeId={id}
                    onChange={(value) => {
                      handleChange("dataExtractionGoal", value);
                    }}
                    value={inputs.dataExtractionGoal}
                    placeholder={placeholders["task"]["dataExtractionGoal"]}
                    className="nopan text-xs"
                  />
                </div>
                <WorkflowDataSchemaInputGroup
                  exampleValue={dataSchemaExampleValue}
                  onChange={(value) => {
                    handleChange("dataSchema", value);
                  }}
                  value={inputs.dataSchema}
                  suggestionContext={{
                    data_extraction_goal: inputs.dataExtractionGoal,
                    current_schema: inputs.dataSchema,
                    navigation_goal: inputs.navigationGoal,
                  }}
                />
              </div>
            </AccordionContent>
          </AccordionItem>
          <AccordionItem value="advanced" className="border-b-0">
            <AccordionTrigger>Advanced Settings</AccordionTrigger>
            <AccordionContent className="pl-6 pr-1 pt-1">
              <div className="space-y-4">
                <div className="space-y-2">
                  <Label className="text-xs text-slate-300">
                    Complete if...
                  </Label>
                  <WorkflowBlockInputTextarea
                    nodeId={id}
                    onChange={(value) => {
                      handleChange("completeCriterion", value);
                    }}
                    value={inputs.completeCriterion}
                    className="nopan text-xs"
                  />
                </div>
                <Separator />
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Max Steps Override
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["task"]["maxStepsOverride"]}
                    />
                  </div>
                  <Input
                    type="number"
                    placeholder={placeholders["task"]["maxStepsOverride"]}
                    className="nopan w-52 text-xs"
                    min="0"
                    value={inputs.maxStepsOverride ?? ""}
                    onChange={(event) => {
                      const value =
                        event.target.value === ""
                          ? null
                          : Number(event.target.value);
                      handleChange("maxStepsOverride", value);
                    }}
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex gap-4">
                    <div className="flex gap-2">
                      <Label className="text-xs font-normal text-slate-300">
                        Error Messages
                      </Label>
                      <HelpTooltip
                        content={helpTooltips["task"]["errorCodeMapping"]}
                      />
                    </div>
                    <Checkbox
                      checked={inputs.errorCodeMapping !== "null"}
                      disabled={!editable}
                      onCheckedChange={(checked) => {
                        handleChange(
                          "errorCodeMapping",
                          checked
                            ? JSON.stringify(errorMappingExampleValue, null, 2)
                            : "null",
                        );
                      }}
                    />
                  </div>
                  {inputs.errorCodeMapping !== "null" && (
                    <div>
                      <CodeEditor
                        language="json"
                        value={inputs.errorCodeMapping}
                        onChange={(value) => {
                          handleChange("errorCodeMapping", value);
                        }}
                        className="nowheel nopan"
                        fontSize={8}
                      />
                    </div>
                  )}
                </div>
                <Separator />
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Continue on Failure
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["task"]["continueOnFailure"]}
                    />
                  </div>
                  <div className="w-52">
                    <Switch
                      checked={inputs.continueOnFailure}
                      onCheckedChange={(checked) => {
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
                      content={helpTooltips["task"]["cacheActions"]}
                    />
                  </div>
                  <div className="w-52">
                    <Switch
                      checked={inputs.cacheActions}
                      onCheckedChange={(checked) => {
                        handleChange("cacheActions", checked);
                      }}
                    />
                  </div>
                </div>
                <Separator />
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Complete on Download
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["task"]["completeOnDownload"]}
                    />
                  </div>
                  <div className="w-52">
                    <Switch
                      checked={inputs.allowDownloads}
                      onCheckedChange={(checked) => {
                        handleChange("allowDownloads", checked);
                      }}
                    />
                  </div>
                </div>
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      File Suffix
                    </Label>
                    <HelpTooltip content={helpTooltips["task"]["fileSuffix"]} />
                  </div>
                  <WorkflowBlockInput
                    nodeId={id}
                    type="text"
                    placeholder={placeholders["task"]["downloadSuffix"]}
                    className="nopan w-52 text-xs"
                    value={inputs.downloadSuffix ?? ""}
                    onChange={(value) => {
                      handleChange("downloadSuffix", value);
                    }}
                  />
                </div>
                <Separator />
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">
                      2FA Identifier
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["task"]["totpIdentifier"]}
                    />
                  </div>
                  <WorkflowBlockInputTextarea
                    nodeId={id}
                    onChange={(value) => {
                      handleChange("totpIdentifier", value);
                    }}
                    value={inputs.totpIdentifier ?? ""}
                    placeholder={placeholders["task"]["totpIdentifier"]}
                    className="nopan text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">
                      2FA Verification URL
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["task"]["totpVerificationUrl"]}
                    />
                  </div>
                  <WorkflowBlockInputTextarea
                    nodeId={id}
                    onChange={(value) => {
                      handleChange("totpVerificationUrl", value);
                    }}
                    value={inputs.totpVerificationUrl ?? ""}
                    placeholder={placeholders["task"]["totpVerificationUrl"]}
                    className="nopan text-xs"
                  />
                </div>
              </div>
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      </div>
    </div>
  );
}

export { TaskNode };
