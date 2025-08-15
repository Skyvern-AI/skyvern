import { useEffect } from "react";
import { Flippable } from "@/components/Flippable";
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
import { BlockCodeEditor } from "@/routes/workflows/components/BlockCodeEditor";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { useBlockScriptStore } from "@/store/BlockScriptStore";
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
import { dataSchemaExampleValue, errorMappingExampleValue } from "../types";
import { ParametersMultiSelect } from "./ParametersMultiSelect";
import type { TaskNode } from "./types";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { RunEngineSelector } from "@/components/EngineSelector";
import { ModelSelector } from "@/components/ModelSelector";
import { useDebugStore } from "@/store/useDebugStore";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";

function TaskNode({ id, data, type }: NodeProps<TaskNode>) {
  const { updateNodeData } = useReactFlow();
  const [facing, setFacing] = useState<"front" | "back">("front");
  const blockScriptStore = useBlockScriptStore();
  const { debuggable, editable, label } = data;
  const script = blockScriptStore.scripts[label];
  const debugStore = useDebugStore();
  const elideFromDebugging = debugStore.isDebugMode && !debuggable;
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(nodes, edges, id);
  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });

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
    includeActionHistoryInVerification: data.includeActionHistoryInVerification,
    engine: data.engine,
    model: data.model,
  });

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
            disabled={elideFromDebugging}
            editable={editable}
            nodeId={id}
            totpIdentifier={inputs.totpIdentifier}
            totpUrl={inputs.totpVerificationUrl}
            type={type}
          />
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
                      canWriteTitle={true}
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
                  <ModelSelector
                    className="nopan w-52 text-xs"
                    value={inputs.model}
                    onChange={(value) => {
                      handleChange("model", value);
                    }}
                  />
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
                              ? JSON.stringify(
                                  errorMappingExampleValue,
                                  null,
                                  2,
                                )
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
                        Include Action History
                      </Label>
                      <HelpTooltip
                        content={
                          helpTooltips["task"][
                            "includeActionHistoryInVerification"
                          ]
                        }
                      />
                    </div>
                    <div className="w-52">
                      <Switch
                        checked={inputs.includeActionHistoryInVerification}
                        onCheckedChange={(checked) => {
                          handleChange(
                            "includeActionHistoryInVerification",
                            checked,
                          );
                        }}
                      />
                    </div>
                  </div>
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
                      <HelpTooltip
                        content={helpTooltips["task"]["fileSuffix"]}
                      />
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
      <BlockCodeEditor blockLabel={label} blockType={type} script={script} />
    </Flippable>
  );
}

export { TaskNode };
