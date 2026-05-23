import { useEdges, useNodes, useNodesData } from "@xyflow/react";

import { RunEngine } from "@/api/types";
import { RunEngineSelector } from "@/components/EngineSelector";
import { HelpTooltip } from "@/components/HelpTooltip";
import { ModelSelector } from "@/components/ModelSelector";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
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
import { ErrorCodeMappingEditor } from "@/routes/workflows/editor/ErrorCodeMappingEditor";

import { AI_IMPROVE_CONFIGS } from "../../constants";
import { helpTooltips, placeholders } from "../../helpContent";
import { useHasInteractedThisSession } from "../../panels/useHasInteractedThisSession";
import { type AppNode } from "..";
import { DisableCache } from "../DisableCache";
import { IgnoreWorkflowSystemPrompt } from "../IgnoreWorkflowSystemPrompt";
import { BlockExecutionOptions } from "../components/BlockExecutionOptions";
import {
  MAX_STEPS_DEFAULT,
  type NavigationNode,
  type NavigationNodeData,
} from "./types";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { errorMappingExampleValue } from "../types";
import { useUpdate } from "../../useUpdate";
import {
  getAvailableOutputParameterKeys,
  getParentLoopSkipsOnFail,
  isNodeInsideForLoop,
} from "../../workflowEditorUtils";

function NavigationEditor({ blockId }: { blockId: string }) {
  // Subscribe to the node's data slice. The sidebar mount lives outside the
  // per-node renderer and the body subscribes to useNodes()/useEdges() for
  // output-parameter discovery; a one-time getNode() snapshot would re-render
  // with stale data after useUpdate commits typed input.
  const nodeSlice = useNodesData<NavigationNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "navigation") {
    return null;
  }
  return <NavigationEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function NavigationEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: NavigationNodeData;
}) {
  const { editable } = data;
  const update = useUpdate<NavigationNodeData>({ id: blockId, editable });
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );
  const isInsideForLoop = isNodeInsideForLoop(nodes, blockId);
  const parentLoopSkipsOnFail = getParentLoopSkipsOnFail(nodes, blockId);
  const isV2Mode = data.engine === RunEngine.SkyvernV2;
  const hasInteracted = useHasInteractedThisSession();

  const handleEngineChange = (value: RunEngine) => {
    if (!editable) return;
    const updates: Partial<NavigationNodeData> = { engine: value };
    if (value === RunEngine.SkyvernV2) {
      updates.prompt = data.navigationGoal || data.prompt;
      updates.navigationGoal = "";
      updates.completeCriterion = "";
      updates.terminateCriterion = "";
      updates.errorCodeMapping = "null";
      updates.parameterKeys = [];
      updates.maxRetries = null;
      updates.maxStepsOverride = null;
      updates.allowDownloads = false;
      updates.downloadSuffix = null;
      updates.includeActionHistoryInVerification = false;
    } else if (data.engine === RunEngine.SkyvernV2) {
      updates.navigationGoal = data.prompt || data.navigationGoal;
      updates.prompt = "";
      updates.maxSteps = MAX_STEPS_DEFAULT;
    }
    update(updates);
  };

  const renderV2Content = () => (
    <>
      <div className="space-y-4">
        <div className="space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">URL</Label>
            <HelpTooltip content={helpTooltips["navigation"]["url"]} />
          </div>
          <WorkflowBlockInputTextarea
            nodeId={blockId}
            onChange={(value) => update({ url: value })}
            value={data.url}
            placeholder={placeholders["taskv2"]["url"]}
            className="nopan text-xs"
          />
        </div>
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Prompt</Label>
          <WorkflowBlockInputTextarea
            aiImprove={AI_IMPROVE_CONFIGS.taskV2.prompt}
            nodeId={blockId}
            onChange={(value) => update({ prompt: value })}
            value={data.prompt}
            placeholder={placeholders["taskv2"]["prompt"]}
            className="nopan text-xs"
          />
        </div>
        <div className="flex items-center justify-between">
          <div className="flex gap-2">
            <Label className="text-xs font-normal text-slate-300">Engine</Label>
            <HelpTooltip content={helpTooltips["navigation"]["engine"]} />
          </div>
          <RunEngineSelector
            value={data.engine}
            onChange={handleEngineChange}
            className="nopan w-72 text-xs"
            availableEngines={[
              RunEngine.SkyvernV1,
              RunEngine.SkyvernV2,
              RunEngine.OpenaiCua,
              RunEngine.AnthropicCua,
            ]}
          />
        </div>
      </div>
      <Separator />
      <Accordion type="single" collapsible>
        <AccordionItem value="advanced" className="border-b-0">
          <AccordionTrigger className="py-0">
            Advanced Settings
          </AccordionTrigger>
          <AccordionContent className="pl-6 pr-1 pt-4">
            <div className="space-y-4">
              <ModelSelector
                className="nopan w-52 text-xs"
                value={data.model}
                onChange={(value) => update({ model: value })}
              />
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">Max Steps</Label>
                  <HelpTooltip content={helpTooltips["taskv2"]["maxSteps"]} />
                </div>
                <Input
                  type="number"
                  placeholder={`${MAX_STEPS_DEFAULT}`}
                  className="nopan text-xs"
                  value={data.maxSteps ?? MAX_STEPS_DEFAULT}
                  onChange={(event) =>
                    update({ maxSteps: Number(event.target.value) })
                  }
                />
              </div>
              <Separator />
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
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">
                    2FA Identifier
                  </Label>
                  <HelpTooltip
                    content={helpTooltips["taskv2"]["totpIdentifier"]}
                  />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={blockId}
                  onChange={(value) => update({ totpIdentifier: value })}
                  value={data.totpIdentifier ?? ""}
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
    </>
  );

  const renderV1Content = () => (
    <>
      <div className="space-y-4">
        <div className="space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">URL</Label>
            <HelpTooltip content={helpTooltips["navigation"]["url"]} />
          </div>
          <WorkflowBlockInputTextarea
            nodeId={blockId}
            onChange={(value) => update({ url: value })}
            value={data.url}
            placeholder={placeholders["navigation"]["url"]}
            className="nopan text-xs"
          />
        </div>
        <div className="space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">Prompt</Label>
            <HelpTooltip
              content={helpTooltips["navigation"]["navigationGoal"]}
            />
          </div>
          <WorkflowBlockInputTextarea
            aiImprove={AI_IMPROVE_CONFIGS.navigation.navigationGoal}
            nodeId={blockId}
            onChange={(value) => update({ navigationGoal: value })}
            value={data.navigationGoal}
            placeholder={placeholders["navigation"]["navigationGoal"]}
            className="nopan text-xs"
          />
        </div>
        {!hasInteracted && (
          <div className="rounded-md bg-slate-800 p-2">
            <div className="space-y-1 text-xs text-slate-400">
              Tip: Try to phrase your prompt as a goal with an explicit
              completion criteria. While executing, Skyvern will take as many
              actions as necessary to accomplish the goal. Use words like
              "Complete" or "Terminate" to help Skyvern identify when it's
              finished or when it should give up.
            </div>
          </div>
        )}
        <div className="flex items-center justify-between">
          <div className="flex gap-2">
            <Label className="text-xs font-normal text-slate-300">Engine</Label>
            <HelpTooltip content={helpTooltips["navigation"]["engine"]} />
          </div>
          <RunEngineSelector
            value={data.engine}
            onChange={handleEngineChange}
            className="nopan w-72 text-xs"
            availableEngines={[
              RunEngine.SkyvernV1,
              RunEngine.SkyvernV2,
              RunEngine.OpenaiCua,
              RunEngine.AnthropicCua,
            ]}
          />
        </div>
      </div>
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
                  onParametersChange={(parameterKeys) =>
                    update({ parameterKeys })
                  }
                  onCredentialTotpIdentifier={(totpIdentifier) => {
                    if (!data.totpIdentifier?.trim()) {
                      update({ totpIdentifier });
                    }
                  }}
                />
              </div>
              <div className="space-y-2">
                <Label className="text-xs text-slate-300">Complete if...</Label>
                <WorkflowBlockInputTextarea
                  aiImprove={AI_IMPROVE_CONFIGS.navigation.completeCriterion}
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
                      content={helpTooltips["navigation"]["errorCodeMapping"]}
                    />
                  </div>
                  <div className="w-52">
                    <Switch
                      checked={data.errorCodeMapping !== "null"}
                      onCheckedChange={(checked) => {
                        if (!editable) return;
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
                blockType="navigation"
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
                  <HelpTooltip
                    content={helpTooltips["navigation"]["fileSuffix"]}
                  />
                </div>
                <WorkflowBlockInput
                  nodeId={blockId}
                  type="text"
                  placeholder={placeholders["navigation"]["downloadSuffix"]}
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
                    content={helpTooltips["navigation"]["totpIdentifier"]}
                  />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={blockId}
                  onChange={(value) => update({ totpIdentifier: value })}
                  value={data.totpIdentifier ?? ""}
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
    </>
  );

  return (
    <div data-testid="navigation-block-form" className="space-y-4">
      {isV2Mode ? renderV2Content() : renderV1Content()}
    </div>
  );
}

export { NavigationEditor };
