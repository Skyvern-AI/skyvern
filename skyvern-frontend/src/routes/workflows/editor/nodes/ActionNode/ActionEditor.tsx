import { useEdges, useNodes, useNodesData } from "@xyflow/react";

import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { RunEngineSelector } from "@/components/EngineSelector";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { ModelSelector } from "@/components/ModelSelector";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { ErrorCodeMappingEditor } from "@/routes/workflows/editor/ErrorCodeMappingEditor";

import { AI_IMPROVE_CONFIGS } from "../../constants";
import { helpTooltips, placeholders } from "../../helpContent";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { useHasInteractedThisSession } from "../../panels/useHasInteractedThisSession";
import { BlockExecutionOptions } from "../components/BlockExecutionOptions";
import { DisableCache } from "../DisableCache";
import { IgnoreWorkflowSystemPrompt } from "../IgnoreWorkflowSystemPrompt";
import { useSelectedCredentialTotpIdentifier } from "../../hooks/useSelectedCredentialTotpIdentifier";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { errorMappingExampleValue } from "../types";
import { type AppNode } from "..";
import { ActionNode, ActionNodeData } from "./types";
import { useUpdate } from "../../useUpdate";
import {
  getAvailableOutputParameterKeys,
  getParentLoopSkipsOnFail,
  isNodeInsideForLoop,
} from "../../workflowEditorUtils";

const urlTooltip =
  "The URL Skyvern is navigating to. Leave this field blank to pick up from where the last block left off.";
const navigationGoalTooltip =
  "Specify a single step or action you'd like Skyvern to complete. Actions are one-off tasks like filling a field or interacting with a specific element on the page.\n\nCurrently supported actions are click, input text, upload file, and select. Use {{ parameter_name }} to specify parameters to use.";
const navigationGoalPlaceholder = 'Input {{ name }} into "Name" field.';

function ActionEditor({ blockId }: { blockId: string }) {
  // Subscribe to the node's data slice. The sidebar mount lives outside the
  // per-node renderer and the body subscribes to useNodes()/useEdges() for
  // output-parameter discovery; a one-time getNode() snapshot would re-render
  // with stale data after useUpdate commits typed input.
  const nodeSlice = useNodesData<ActionNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "action") {
    return null;
  }
  return <ActionEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function ActionEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: ActionNodeData;
}) {
  const { editable } = data;
  const update = useUpdate<ActionNodeData>({ id: blockId, editable });
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
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
    data.parameterKeys.length > 0 ? data.parameterKeys[0] : undefined,
  );

  return (
    <div data-testid="action-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex justify-between">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">URL</Label>
            <HelpTooltip content={urlTooltip} />
          </div>
          {isFirstWorkflowBlock ? (
            <div className="flex justify-end text-xs text-slate-400">
              Tip: Use the {"+"} button to add parameters!
            </div>
          ) : null}
        </div>
        <WorkflowBlockInputTextarea
          nodeId={blockId}
          onChange={(value) => update({ url: value })}
          value={data.url}
          placeholder={placeholders["action"]["url"]}
          className="nopan text-xs"
        />
      </div>
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-slate-300">Action Instruction</Label>
          <HelpTooltip content={navigationGoalTooltip} />
        </div>
        <WorkflowBlockInputTextarea
          aiImprove={AI_IMPROVE_CONFIGS.action.navigationGoal}
          nodeId={blockId}
          onChange={(value) => update({ navigationGoal: value })}
          value={data.navigationGoal}
          placeholder={navigationGoalPlaceholder}
          className="nopan text-xs"
        />
      </div>
      {!hasInteracted && (
        <div className="rounded-md bg-slate-800 p-2">
          <div className="space-y-1 text-xs text-slate-400">
            Tip: While executing the action block, Skyvern will only take one
            action.
          </div>
        </div>
      )}
      <Separator />
      <Accordion type="single" collapsible>
        <AccordionItem value="advanced" className="border-b-0">
          <AccordionTrigger className="py-0">
            Advanced Settings
          </AccordionTrigger>
          <AccordionContent className="pl-6 pr-1 pt-1">
            <div className="space-y-4">
              <div className="space-y-2">
                <ModelSelector
                  className="nopan w-52 text-xs"
                  value={data.model}
                  onChange={(value) => update({ model: value })}
                />
                <ParametersMultiSelect
                  availableOutputParameters={outputParameterKeys}
                  parameters={data.parameterKeys}
                  onParametersChange={(parameterKeys) =>
                    update({ parameterKeys })
                  }
                />
              </div>
              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs font-normal text-slate-300">
                    Engine
                  </Label>
                  <HelpTooltip content={helpTooltips["action"]["engine"]} />
                </div>
                <RunEngineSelector
                  value={data.engine}
                  onChange={(value) => update({ engine: value })}
                  className="nopan w-52 text-xs"
                />
              </div>
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Error Messages
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["action"]["errorCodeMapping"]}
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
                editable={editable}
                isInsideForLoop={isInsideForLoop}
                parentLoopSkipsOnFail={parentLoopSkipsOnFail}
                blockType="action"
                onContinueOnFailureChange={(checked) =>
                  update({ continueOnFailure: checked })
                }
                onNextLoopOnFailureChange={(checked) =>
                  update({ nextLoopOnFailure: checked })
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
                    content={helpTooltips["action"]["completeOnDownload"]}
                  />
                </div>
                <div className="w-52">
                  <Switch
                    checked={data.allowDownloads}
                    onCheckedChange={(checked) => {
                      if (!editable) {
                        return;
                      }
                      update({ allowDownloads: checked });
                    }}
                  />
                </div>
              </div>
              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs font-normal text-slate-300">
                    File Name
                  </Label>
                  <HelpTooltip content={helpTooltips["action"]["fileSuffix"]} />
                </div>
                <WorkflowBlockInput
                  nodeId={blockId}
                  type="text"
                  placeholder={placeholders["action"]["downloadSuffix"]}
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
                    content={helpTooltips["action"]["totpIdentifier"]}
                  />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={blockId}
                  onChange={(value) => update({ totpIdentifier: value })}
                  value={data.totpIdentifier ?? ""}
                  placeholder={
                    !data.totpIdentifier?.trim() && credentialTotpIdentifier
                      ? `${credentialTotpIdentifier} (from credential)`
                      : placeholders["action"]["totpIdentifier"]
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

export { ActionEditor };
