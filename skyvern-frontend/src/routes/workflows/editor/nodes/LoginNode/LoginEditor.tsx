import { useEdges, useNodes, useNodesData } from "@xyflow/react";

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
import { RunEngineSelector } from "@/components/EngineSelector";
import { ModelSelector } from "@/components/ModelSelector";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";

import { ErrorCodeMappingEditor } from "../../ErrorCodeMappingEditor";
import { AI_IMPROVE_CONFIGS } from "../../constants";
import { helpTooltips, placeholders } from "../../helpContent";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { type AppNode } from "..";
import { BlockExecutionOptions } from "../components/BlockExecutionOptions";
import { DisableCache } from "../DisableCache";
import { IgnoreWorkflowSystemPrompt } from "../IgnoreWorkflowSystemPrompt";
import { LoginBlockCredentialSelector } from "./LoginBlockCredentialSelector";
import { useSelectedCredentialTotpIdentifier } from "../../hooks/useSelectedCredentialTotpIdentifier";
import { type LoginNode, type LoginNodeData } from "./types";
import { errorMappingExampleValue } from "../types";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { useUpdate } from "../../useUpdate";
import {
  getAvailableOutputParameterKeys,
  getParentLoopSkipsOnFail,
  isNodeInsideForLoop,
} from "../../workflowEditorUtils";

function LoginEditor({ blockId }: { blockId: string }) {
  // Subscribe to the node's data slice. The sidebar mount lives outside the
  // per-node renderer and the body subscribes to useNodes()/useEdges() for
  // output-parameter discovery; a one-time getNode() snapshot would re-render
  // with stale data after useUpdate commits typed input.
  const nodeSlice = useNodesData<LoginNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "login") {
    return null;
  }
  return <LoginEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function LoginEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: LoginNodeData;
}) {
  const { editable } = data;
  const update = useUpdate<LoginNodeData>({ id: blockId, editable });
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
  const credentialTotpIdentifier = useSelectedCredentialTotpIdentifier(
    data.parameterKeys.length > 0 ? data.parameterKeys[0] : undefined,
  );

  return (
    <div data-testid="login-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex justify-between">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">URL</Label>
            <HelpTooltip content={helpTooltips["login"]["url"]} />
          </div>
          {isFirstWorkflowBlock ? (
            <div className="flex justify-end text-xs text-slate-400">
              Tip: Use the {"+"} button to add inputs!
            </div>
          ) : null}
        </div>
        <WorkflowBlockInputTextarea
          nodeId={blockId}
          onChange={(value) => update({ url: value })}
          value={data.url}
          placeholder={placeholders["login"]["url"]}
          className="nopan text-xs"
        />
      </div>
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-slate-300">Login Goal</Label>
          <HelpTooltip content={helpTooltips["login"]["navigationGoal"]} />
        </div>
        <WorkflowBlockInputTextarea
          aiImprove={AI_IMPROVE_CONFIGS.login.navigationGoal}
          nodeId={blockId}
          onChange={(value) => update({ navigationGoal: value })}
          value={data.navigationGoal}
          placeholder={placeholders["login"]["navigationGoal"]}
          className="nopan text-xs"
        />
      </div>
      <div className="space-y-3 rounded-md border border-slate-700/50 bg-slate-900/30 p-3">
        <p className="text-xs font-medium text-slate-300">Authentication</p>
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Credential</Label>
          <p className="text-[0.7rem] text-slate-400">
            Credentials are encrypted server-side; secret values never echo back
            into the editor.
          </p>
          <LoginBlockCredentialSelector
            nodeId={blockId}
            value={
              data.parameterKeys.length > 0 ? data.parameterKeys[0] : undefined
            }
            onChange={(value) => {
              if (!editable) return;
              // Preserve any extra parameter keys the user added via the
              // Advanced > Parameters multi-select; only replace the
              // credential slot (always at index 0). Without this merge a
              // credential pick / swap silently wipes downstream parameter
              // additions.
              const otherKeys = data.parameterKeys.slice(1);
              update({ parameterKeys: [value, ...otherKeys] });
            }}
            currentUrl={data.url}
            onUrlAutoFill={(url) => {
              if (editable) update({ url });
            }}
          />
        </div>
        <div className="space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">2FA Identifier</Label>
            <HelpTooltip content={helpTooltips["login"]["totpIdentifier"]} />
          </div>
          <WorkflowBlockInputTextarea
            nodeId={blockId}
            onChange={(value) => update({ totpIdentifier: value })}
            value={data.totpIdentifier ?? ""}
            placeholder={
              !data.totpIdentifier?.trim() && credentialTotpIdentifier
                ? `${credentialTotpIdentifier} (from credential)`
                : placeholders["login"]["totpIdentifier"]
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
              content={helpTooltips["login"]["totpVerificationUrl"]}
            />
          </div>
          <WorkflowBlockInputTextarea
            nodeId={blockId}
            onChange={(value) => update({ totpVerificationUrl: value })}
            value={data.totpVerificationUrl ?? ""}
            placeholder={placeholders["login"]["totpVerificationUrl"]}
            className="nopan text-xs"
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
              <div className="space-y-2">
                <Label className="text-xs text-slate-300">Complete if...</Label>
                <WorkflowBlockInputTextarea
                  aiImprove={AI_IMPROVE_CONFIGS.login.completeCriterion}
                  nodeId={blockId}
                  onChange={(value) => update({ completeCriterion: value })}
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
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Error Messages
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["login"]["errorCodeMapping"]}
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
                editable={editable}
                isInsideForLoop={isInsideForLoop}
                parentLoopSkipsOnFail={parentLoopSkipsOnFail}
                blockType="login"
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
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

export { LoginEditor };
