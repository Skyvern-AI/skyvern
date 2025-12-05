import { useEffect, useState } from "react";
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
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { BlockCodeEditor } from "@/routes/workflows/components/BlockCodeEditor";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { useBlockScriptStore } from "@/store/BlockScriptStore";
import { Handle, NodeProps, Position, useEdges, useNodes } from "@xyflow/react";
import { helpTooltips, placeholders } from "../../helpContent";
import { errorMappingExampleValue } from "../types";
import type { LoginNode } from "./types";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { AppNode } from "..";
import {
  getAvailableOutputParameterKeys,
  isNodeInsideForLoop,
} from "../../workflowEditorUtils";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { LoginBlockCredentialSelector } from "./LoginBlockCredentialSelector";
import { RunEngineSelector } from "@/components/EngineSelector";
import { ModelSelector } from "@/components/ModelSelector";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useUpdate } from "@/routes/workflows/editor/useUpdate";
import { useRerender } from "@/hooks/useRerender";

import { DisableCache } from "../DisableCache";
import { BlockExecutionOptions } from "../components/BlockExecutionOptions";
import { AI_IMPROVE_CONFIGS } from "../../constants";

function LoginNode({ id, data, type }: NodeProps<LoginNode>) {
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
  const rerender = useRerender({ prefix: "accordian" });
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(nodes, edges, id);
  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });
  const update = useUpdate<LoginNode["data"]>({ id, editable });
  const isInsideForLoop = isNodeInsideForLoop(nodes, id);

  // Manage flippable facing state
  const [facing, setFacing] = useState<"front" | "back">("front");
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
            data.comparisonColor,
          )}
        >
          <NodeHeader
            blockLabel={label}
            editable={editable}
            nodeId={id}
            totpIdentifier={data.totpIdentifier}
            totpUrl={data.totpVerificationUrl}
            type={type}
          />
          <div className="space-y-4">
            <div className="space-y-2">
              <div className="flex justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">URL</Label>
                  <HelpTooltip content={helpTooltips["login"]["url"]} />
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
                onChange={(value) => update({ url: value })}
                value={data.url}
                placeholder={placeholders["login"]["url"]}
                className="nopan text-xs"
              />
            </div>
            <div className="space-y-2">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">Login Goal</Label>
                <HelpTooltip
                  content={helpTooltips["login"]["navigationGoal"]}
                />
              </div>
              <WorkflowBlockInputTextarea
                aiImprove={AI_IMPROVE_CONFIGS.login.navigationGoal}
                nodeId={id}
                onChange={(value) => {
                  update({ navigationGoal: value });
                }}
                value={data.navigationGoal}
                placeholder={placeholders["login"]["navigationGoal"]}
                className="nopan text-xs"
              />
            </div>
            <div className="space-y-2">
              <Label className="text-xs text-slate-300">Credential</Label>
              <LoginBlockCredentialSelector
                nodeId={id}
                value={
                  data.parameterKeys.length > 0
                    ? data.parameterKeys[0]
                    : undefined
                }
                onChange={(value) => {
                  if (!editable) {
                    return;
                  }
                  update({ parameterKeys: [value] });
                }}
              />
            </div>
          </div>
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
              <AccordionContent key={rerender.key} className="pl-6 pr-1 pt-1">
                <div className="space-y-4">
                  <div className="space-y-2">
                    <ModelSelector
                      className="nopan w-52 text-xs"
                      value={data.model}
                      onChange={(value) => {
                        update({ model: value });
                      }}
                    />
                    <ParametersMultiSelect
                      availableOutputParameters={outputParameterKeys}
                      parameters={data.parameterKeys}
                      onParametersChange={(parameterKeys) => {
                        update({ parameterKeys });
                      }}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label className="text-xs text-slate-300">
                      Complete if...
                    </Label>
                    <WorkflowBlockInputTextarea
                      aiImprove={AI_IMPROVE_CONFIGS.login.completeCriterion}
                      nodeId={id}
                      onChange={(value) => {
                        update({ completeCriterion: value });
                      }}
                      value={data.completeCriterion}
                      className="nopan text-xs"
                    />
                  </div>
                  <Separator />
                  <div className="flex items-center justify-between">
                    <div className="flex gap-2">
                      <Label className="text-xs font-normal text-slate-300">
                        Engine
                      </Label>
                    </div>
                    <RunEngineSelector
                      value={data.engine}
                      onChange={(value) => {
                        update({ engine: value });
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
                        content={helpTooltips["login"]["maxStepsOverride"]}
                      />
                    </div>
                    <Input
                      type="number"
                      placeholder={placeholders["login"]["maxStepsOverride"]}
                      className="nopan w-52 text-xs"
                      min="0"
                      value={data.maxStepsOverride ?? ""}
                      onChange={(event) => {
                        const value =
                          event.target.value === ""
                            ? null
                            : Number(event.target.value);
                        update({ maxStepsOverride: value });
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
                          content={helpTooltips["login"]["errorCodeMapping"]}
                        />
                      </div>
                      <Checkbox
                        checked={data.errorCodeMapping !== "null"}
                        disabled={!editable}
                        onCheckedChange={(checked) => {
                          update({
                            errorCodeMapping: checked
                              ? JSON.stringify(
                                  errorMappingExampleValue,
                                  null,
                                  2,
                                )
                              : "null",
                          });
                        }}
                      />
                    </div>
                    {data.errorCodeMapping !== "null" && (
                      <div>
                        <CodeEditor
                          language="json"
                          value={data.errorCodeMapping}
                          onChange={(value) => {
                            update({ errorCodeMapping: value });
                          }}
                          className="nopan"
                          fontSize={8}
                        />
                      </div>
                    )}
                  </div>
                  <BlockExecutionOptions
                    continueOnFailure={data.continueOnFailure}
                    nextLoopOnFailure={data.nextLoopOnFailure}
                    editable={editable}
                    isInsideForLoop={isInsideForLoop}
                    blockType="login"
                    onContinueOnFailureChange={(checked) => {
                      update({ continueOnFailure: checked });
                    }}
                    onNextLoopOnFailureChange={(checked) => {
                      update({ nextLoopOnFailure: checked });
                    }}
                  />
                  <DisableCache
                    disableCache={data.disableCache}
                    editable={editable}
                    onDisableCacheChange={(disableCache) => {
                      update({ disableCache });
                    }}
                  />
                  <Separator />
                  <div className="space-y-2">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">
                        2FA Identifier
                      </Label>
                      <HelpTooltip
                        content={helpTooltips["login"]["totpIdentifier"]}
                      />
                    </div>
                    <WorkflowBlockInputTextarea
                      nodeId={id}
                      onChange={(value) => {
                        update({ totpIdentifier: value });
                      }}
                      value={data.totpIdentifier ?? ""}
                      placeholder={placeholders["login"]["totpIdentifier"]}
                      className="nopan text-xs"
                    />
                  </div>
                  <div className="space-y-2">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">
                        2FA Verification URL
                      </Label>
                      <HelpTooltip
                        content={helpTooltips["login"]["totpVerificationUrl"]}
                      />
                    </div>
                    <WorkflowBlockInputTextarea
                      nodeId={id}
                      onChange={(value) => {
                        update({ totpVerificationUrl: value });
                      }}
                      value={data.totpVerificationUrl ?? ""}
                      placeholder={placeholders["login"]["totpVerificationUrl"]}
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

export { LoginNode };
