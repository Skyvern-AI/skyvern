import { useEdges, useNodes } from "@xyflow/react";

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
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";
import { RunEngineSelector } from "@/components/EngineSelector";
import { ModelSelector } from "@/components/ModelSelector";
import { ErrorCodeMappingEditor } from "@/routes/workflows/editor/ErrorCodeMappingEditor";

import { AI_IMPROVE_CONFIGS } from "../../constants";
import { helpTooltips, placeholders } from "../../helpContent";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { useHasInteractedThisSession } from "../../panels/useHasInteractedThisSession";
import { type AppNode, isWorkflowBlockNode } from "..";
import { BlockExecutionOptions } from "../components/BlockExecutionOptions";
import { DisableCache } from "../DisableCache";
import { IgnoreWorkflowSystemPrompt } from "../IgnoreWorkflowSystemPrompt";
import { useSelectedCredentialTotpIdentifier } from "../../hooks/useSelectedCredentialTotpIdentifier";
import { ParametersMultiSelect } from "./ParametersMultiSelect";
import type { TaskNode, TaskNodeData } from "./types";
import { dataSchemaExampleValue, errorMappingExampleValue } from "../types";
import { useUpdate } from "../../useUpdate";
import {
  getAvailableOutputParameterKeys,
  getParentLoopSkipsOnFail,
  isNodeInsideForLoop,
} from "../../workflowEditorUtils";

function TaskEditor({ blockId }: { blockId: string }) {
  return <TaskEditorBody blockId={blockId} />;
}

function TaskEditorBody({ blockId }: { blockId: string }) {
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  // Subscribe to the node via the live useNodes() store rather than a
  // snapshot from reactFlowInstance.getNode - sidebar editors mount via
  // BlockConfigForm (not the RF node renderer), so a getNode snapshot
  // would not update after updateNodeData replaces the node object,
  // snapping controlled fields back to stale values.
  const node = nodes.find((n) => n.id === blockId);
  const taskNode =
    node && isWorkflowBlockNode(node) && node.type === "task"
      ? (node as TaskNode)
      : null;
  const data = taskNode?.data as TaskNodeData | undefined;
  const editable = data?.editable ?? false;
  const update = useUpdate<TaskNodeData>({ id: blockId, editable });
  const outputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );
  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id: blockId });
  const isInsideForLoop = isNodeInsideForLoop(nodes, blockId);
  const parentLoopSkipsOnFail = getParentLoopSkipsOnFail(nodes, blockId);
  const hasInteracted = useHasInteractedThisSession();
  const credentialTotpIdentifier = useSelectedCredentialTotpIdentifier(
    data?.parameterKeys?.[0],
  );
  if (!taskNode || !data) {
    return null;
  }

  return (
    <div
      data-testid="task-block-form"
      data-block-id={blockId}
      className="space-y-4"
    >
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
                  {isFirstWorkflowBlock && !hasInteracted ? (
                    <div className="flex justify-end text-xs text-slate-400">
                      Tip: Type {"{{"} to reference a parameter
                    </div>
                  ) : null}
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={blockId}
                  onChange={(value) => update({ url: value })}
                  value={data.url}
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
                  aiImprove={AI_IMPROVE_CONFIGS.task.navigationGoal}
                  nodeId={blockId}
                  onChange={(value) => update({ navigationGoal: value })}
                  value={data.navigationGoal}
                  placeholder={placeholders["task"]["navigationGoal"]}
                  className="nopan text-xs"
                />
              </div>
              <div className="space-y-2">
                <ParametersMultiSelect
                  availableOutputParameters={outputParameterKeys}
                  parameters={data.parameterKeys}
                  onParametersChange={(parameterKeys) =>
                    update({ parameterKeys })
                  }
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
                  aiImprove={{
                    useCase: AI_IMPROVE_CONFIGS.task.dataExtractionGoal.useCase,
                    context: {
                      ...AI_IMPROVE_CONFIGS.task.dataExtractionGoal.context,
                      data_schema:
                        data.dataSchema && data.dataSchema !== "null"
                          ? data.dataSchema
                          : undefined,
                      navigation_goal: data.navigationGoal || undefined,
                    },
                  }}
                  nodeId={blockId}
                  onChange={(value) => update({ dataExtractionGoal: value })}
                  value={data.dataExtractionGoal}
                  placeholder={placeholders["task"]["dataExtractionGoal"]}
                  className="nopan text-xs"
                />
              </div>
              <WorkflowDataSchemaInputGroup
                exampleValue={dataSchemaExampleValue}
                onChange={(value) => update({ dataSchema: value })}
                value={data.dataSchema}
                suggestionContext={{
                  data_extraction_goal: data.dataExtractionGoal,
                  current_schema: data.dataSchema,
                  navigation_goal: data.navigationGoal,
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
                <Label className="text-xs text-slate-300">Complete if...</Label>
                <WorkflowBlockInputTextarea
                  aiImprove={AI_IMPROVE_CONFIGS.task.completeCriterion}
                  nodeId={blockId}
                  onChange={(value) => update({ completeCriterion: value })}
                  value={data.completeCriterion}
                  className="nopan text-xs"
                />
              </div>
              <Separator />
              <ModelSelector
                className="nopan w-52 text-xs"
                value={data.model}
                onChange={(value) => update({ model: value })}
              />
              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs font-normal text-slate-300">
                    Engine
                  </Label>
                  <HelpTooltip content={helpTooltips["task"]["engine"]} />
                </div>
                <RunEngineSelector
                  value={data.engine}
                  onChange={(value) => update({ engine: value })}
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
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Error Messages
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["task"]["errorCodeMapping"]}
                    />
                  </div>
                  <div className="w-52">
                    <Switch
                      checked={data.errorCodeMapping !== "null"}
                      onCheckedChange={(checked) => {
                        if (!editable) {
                          return;
                        }
                        update({
                          errorCodeMapping: checked
                            ? JSON.stringify(errorMappingExampleValue, null, 2)
                            : "null",
                        });
                      }}
                    />
                  </div>
                </div>
                {data.errorCodeMapping !== "null" && (
                  <ErrorCodeMappingEditor
                    label={data.label}
                    value={data.errorCodeMapping}
                    onChange={(value) => update({ errorCodeMapping: value })}
                    readOnly={!editable}
                  />
                )}
              </div>
              <BlockExecutionOptions
                continueOnFailure={data.continueOnFailure}
                nextLoopOnFailure={data.nextLoopOnFailure}
                includeActionHistoryInVerification={
                  data.includeActionHistoryInVerification
                }
                editable={editable}
                isInsideForLoop={isInsideForLoop}
                parentLoopSkipsOnFail={parentLoopSkipsOnFail}
                blockType="task"
                showOptions={{
                  continueOnFailure: true,
                  nextLoopOnFailure: true,
                  includeActionHistoryInVerification: true,
                }}
                onContinueOnFailureChange={(checked) =>
                  update({ continueOnFailure: checked })
                }
                onNextLoopOnFailureChange={(checked) =>
                  update({ nextLoopOnFailure: checked })
                }
                onIncludeActionHistoryInVerificationChange={(checked) =>
                  update({ includeActionHistoryInVerification: checked })
                }
              />
              <DisableCache
                disableCache={data.disableCache}
                editable={editable}
                onDisableCacheChange={(disableCache) =>
                  update({ disableCache })
                }
              />
              <IgnoreWorkflowSystemPrompt
                ignoreWorkflowSystemPrompt={
                  data.ignoreWorkflowSystemPrompt ?? false
                }
                editable={editable}
                onIgnoreWorkflowSystemPromptChange={(
                  ignoreWorkflowSystemPrompt,
                ) => {
                  update({ ignoreWorkflowSystemPrompt });
                }}
              />
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
                    checked={data.allowDownloads}
                    onCheckedChange={(checked) =>
                      update({ allowDownloads: checked })
                    }
                  />
                </div>
              </div>
              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs font-normal text-slate-300">
                    File Name
                  </Label>
                  <HelpTooltip content={helpTooltips["task"]["fileSuffix"]} />
                </div>
                <WorkflowBlockInput
                  nodeId={blockId}
                  type="text"
                  placeholder={placeholders["task"]["downloadSuffix"]}
                  className="nopan w-52 text-xs"
                  value={data.downloadSuffix ?? ""}
                  onChange={(value) => update({ downloadSuffix: value })}
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
                  nodeId={blockId}
                  onChange={(value) => update({ totpIdentifier: value })}
                  value={data.totpIdentifier ?? ""}
                  placeholder={
                    !data.totpIdentifier?.trim() && credentialTotpIdentifier
                      ? `${credentialTotpIdentifier} (from credential)`
                      : placeholders["task"]["totpIdentifier"]
                  }
                  className="nopan text-xs"
                />
                {!data.totpIdentifier?.trim() && credentialTotpIdentifier ? (
                  <p className="text-xs text-slate-500">
                    Leave empty to use the credential's value.
                  </p>
                ) : null}
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
                  nodeId={blockId}
                  onChange={(value) => update({ totpVerificationUrl: value })}
                  value={data.totpVerificationUrl ?? ""}
                  placeholder={placeholders["task"]["totpVerificationUrl"]}
                  className="nopan text-xs"
                />
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

export { TaskEditor };
