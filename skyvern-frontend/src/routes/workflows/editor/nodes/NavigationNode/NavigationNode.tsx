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
import {
  Handle,
  NodeProps,
  Position,
  useEdges,
  useNodes,
  useReactFlow,
} from "@xyflow/react";
import { useState } from "react";
import { helpTooltips, placeholders } from "../../helpContent";
import { errorMappingExampleValue } from "../types";
import type { NavigationNode } from "./types";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { AppNode } from "..";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { RunEngineSelector } from "@/components/EngineSelector";
import { ModelSelector } from "@/components/ModelSelector";
import { useDebugStore } from "@/store/useDebugStore";
import { cn } from "@/util/utils";
import { useParams } from "react-router-dom";
import { NodeHeader } from "../components/NodeHeader";

function NavigationNode({ id, data, type }: NodeProps<NavigationNode>) {
  const { blockLabel: urlBlockLabel } = useParams();
  const debugStore = useDebugStore();
  const { updateNodeData } = useReactFlow();
  const { editable, debuggable, label } = data;
  const thisBlockIsPlaying =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const elideFromDebugging = debugStore.isDebugMode && !debuggable;
  const [inputs, setInputs] = useState({
    allowDownloads: data.allowDownloads,
    cacheActions: data.cacheActions,
    completeCriterion: data.completeCriterion,
    continueOnFailure: data.continueOnFailure,
    downloadSuffix: data.downloadSuffix,
    engine: data.engine,
    errorCodeMapping: data.errorCodeMapping,
    includeActionHistoryInVerification: data.includeActionHistoryInVerification,
    maxStepsOverride: data.maxStepsOverride,
    model: data.model,
    navigationGoal: data.navigationGoal,
    terminateCriterion: data.terminateCriterion,
    totpIdentifier: data.totpIdentifier,
    totpVerificationUrl: data.totpVerificationUrl,
    url: data.url,
  });

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
          editable={editable}
          disabled={elideFromDebugging}
          nodeId={id}
          totpIdentifier={inputs.totpIdentifier}
          totpUrl={inputs.totpVerificationUrl}
          type={type}
        />
        <div
          className={cn("space-y-4", {
            "opacity-50": thisBlockIsPlaying,
          })}
        >
          <div className="space-y-2">
            <div className="flex justify-between">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">URL</Label>
                <HelpTooltip content={helpTooltips["navigation"]["url"]} />
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
              placeholder={placeholders["navigation"]["url"]}
              className="nopan text-xs"
            />
          </div>
          <div className="space-y-2">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">Navigation Goal</Label>
              <HelpTooltip
                content={helpTooltips["navigation"]["navigationGoal"]}
              />
            </div>
            <WorkflowBlockInputTextarea
              nodeId={id}
              onChange={(value) => {
                handleChange("navigationGoal", value);
              }}
              value={inputs.navigationGoal}
              placeholder={placeholders["navigation"]["navigationGoal"]}
              className="nopan text-xs"
            />
          </div>
          <div className="rounded-md bg-slate-800 p-2">
            <div className="space-y-1 text-xs text-slate-400">
              Tip: Try to phrase your prompt as a goal with an explicit
              completion criteria. While executing, Skyvern will take as many
              actions as necessary to accomplish the goal. Use words like
              "Complete" or "Terminate" to help Skyvern identify when it's
              finished or when it should give up.
            </div>
          </div>
        </div>
        <Separator />
        <Accordion
          className={cn({
            "pointer-events-none opacity-50": thisBlockIsPlaying,
          })}
          type="single"
          collapsible
        >
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
                      content={helpTooltips["navigation"]["maxStepsOverride"]}
                    />
                  </div>
                  <Input
                    type="number"
                    placeholder={placeholders["navigation"]["maxStepsOverride"]}
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
                        content={helpTooltips["navigation"]["errorCodeMapping"]}
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
                      Include Action History
                    </Label>
                    <HelpTooltip
                      content={
                        helpTooltips["navigation"][
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
                      content={helpTooltips["navigation"]["continueOnFailure"]}
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
                      content={helpTooltips["navigation"]["cacheActions"]}
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
                      content={helpTooltips["navigation"]["completeOnDownload"]}
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
                      content={helpTooltips["navigation"]["fileSuffix"]}
                    />
                  </div>
                  <WorkflowBlockInput
                    nodeId={id}
                    type="text"
                    placeholder={placeholders["navigation"]["downloadSuffix"]}
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
                      content={helpTooltips["navigation"]["totpIdentifier"]}
                    />
                  </div>
                  <WorkflowBlockInputTextarea
                    nodeId={id}
                    onChange={(value) => {
                      handleChange("totpIdentifier", value);
                    }}
                    value={inputs.totpIdentifier ?? ""}
                    placeholder={placeholders["navigation"]["totpIdentifier"]}
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

export { NavigationNode };
