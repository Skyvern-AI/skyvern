import { useEdges, useNodes, useNodesData } from "@xyflow/react";

import { BROWSER_DOWNLOAD_TIMEOUT_SECONDS } from "@/api/types";
import { RunEngineSelector } from "@/components/EngineSelector";
import { HelpTooltip } from "@/components/HelpTooltip";
import { ModelSelector } from "@/components/ModelSelector";
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

import { ErrorCodeMappingEditor } from "../../ErrorCodeMappingEditor";
import { AI_IMPROVE_CONFIGS } from "../../constants";
import { helpTooltips, placeholders } from "../../helpContent";
import { useHasInteractedThisSession } from "../../panels/useHasInteractedThisSession";
import { type AppNode } from "..";
import { DisableCache } from "../DisableCache";
import { IgnoreWorkflowSystemPrompt } from "../IgnoreWorkflowSystemPrompt";
import { BlockExecutionOptions } from "../components/BlockExecutionOptions";
import { type FileDownloadNode, type FileDownloadNodeData } from "./types";
import { useSelectedCredentialTotpIdentifier } from "../../hooks/useSelectedCredentialTotpIdentifier";
import { errorMappingExampleValue } from "../types";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { useUpdate } from "../../useUpdate";
import {
  getAvailableOutputParameterKeys,
  getParentLoopSkipsOnFail,
  isNodeInsideForLoop,
} from "../../workflowEditorUtils";

const urlTooltip =
  "The URL Skyvern is navigating to. Leave this field blank to pick up from where the last block left off.";
const urlPlaceholder = "https://";
const navigationGoalTooltip =
  "Give Skyvern an objective that describes how to download the file.";
const navigationGoalPlaceholder = "Tell Skyvern which file to download.";

function FileDownloadEditor({ blockId }: { blockId: string }) {
  // Subscribe to this node's data slice. The sidebar mount lives outside the
  // per-node renderer, so a useReactFlow().getNode(id) snapshot does not
  // re-render after updateNodeData commits typed input.
  const nodeSlice = useNodesData<FileDownloadNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "fileDownload") {
    return null;
  }
  return <FileDownloadEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function FileDownloadEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: FileDownloadNodeData;
}) {
  const {
    editable,
    label,
    url,
    navigationGoal,
    downloadTimeout,
    model,
    parameterKeys,
    engine,
    maxStepsOverride,
    errorCodeMapping,
    continueOnFailure,
    nextLoopOnFailure,
    disableCache,
    downloadSuffix,
    totpIdentifier,
    totpVerificationUrl,
  } = data;

  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );
  const isInsideForLoop = isNodeInsideForLoop(nodes, blockId);
  const parentLoopSkipsOnFail = getParentLoopSkipsOnFail(nodes, blockId);
  const update = useUpdate<FileDownloadNodeData>({ id: blockId, editable });
  const hasInteracted = useHasInteractedThisSession();
  const credentialTotpIdentifier = useSelectedCredentialTotpIdentifier(
    parameterKeys.length > 0 ? parameterKeys[0] : undefined,
  );

  return (
    <div data-testid="file-download-block-form" className="space-y-4">
      <div className="space-y-4">
        <div className="space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">URL</Label>
            <HelpTooltip content={urlTooltip} />
          </div>
          <WorkflowBlockInputTextarea
            nodeId={blockId}
            onChange={(next) => update({ url: next })}
            value={url}
            placeholder={urlPlaceholder}
            className="nopan text-xs"
          />
        </div>
        <div className="space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">Download Goal</Label>
            <HelpTooltip content={navigationGoalTooltip} />
          </div>
          <WorkflowBlockInputTextarea
            aiImprove={AI_IMPROVE_CONFIGS.fileDownload.navigationGoal}
            nodeId={blockId}
            onChange={(next) => update({ navigationGoal: next })}
            value={navigationGoal}
            placeholder={navigationGoalPlaceholder}
            className="nopan text-xs"
          />
        </div>
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <Label className="text-xs text-slate-300">
              Download Timeout (sec)
            </Label>
            <HelpTooltip
              content={`The maximum time to wait for downloads to complete, in seconds. If not set, defaults to ${BROWSER_DOWNLOAD_TIMEOUT_SECONDS} seconds.`}
            />
            <Input
              className="ml-auto w-16 text-right"
              type="number"
              min={1}
              value={downloadTimeout ?? ""}
              placeholder={`${BROWSER_DOWNLOAD_TIMEOUT_SECONDS}`}
              onChange={(event) => {
                // Empty input clears the override; numeric inputs land
                // verbatim. The previous `if (next) { update(...) }`
                // silently dropped `0`, so a user typing "0" thinking
                // "no timeout" got no save and no feedback.
                if (event.target.value === "") {
                  update({ downloadTimeout: null });
                  return;
                }
                const next = Number(event.target.value);
                if (Number.isFinite(next) && next >= 0) {
                  update({ downloadTimeout: next });
                }
              }}
            />
          </div>
        </div>
        {!hasInteracted && (
          <div className="workflow-editor-tip rounded-md bg-slate-800 p-2 text-xs text-slate-400">
            Once the file is downloaded, this block will complete.
          </div>
        )}
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
                  value={model}
                  onChange={(next) => update({ model: next })}
                />
                <ParametersMultiSelect
                  availableOutputParameters={outputParameterKeys}
                  parameters={parameterKeys}
                  onParametersChange={(next) => update({ parameterKeys: next })}
                />
              </div>
              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs font-normal text-slate-300">
                    Engine
                  </Label>
                </div>
                <RunEngineSelector
                  value={engine}
                  onChange={(next) => update({ engine: next })}
                  className="nopan w-52 text-xs"
                />
              </div>
              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs font-normal text-slate-300">
                    Max Steps Override
                  </Label>
                  <HelpTooltip
                    content={helpTooltips["download"]["maxStepsOverride"]}
                  />
                </div>
                <Input
                  type="number"
                  placeholder={placeholders["download"]["maxStepsOverride"]}
                  className="nopan w-52 text-xs"
                  min="0"
                  value={maxStepsOverride ?? ""}
                  onChange={(event) => {
                    const next =
                      event.target.value === ""
                        ? null
                        : Number(event.target.value);
                    update({ maxStepsOverride: next });
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
                      content={helpTooltips["download"]["errorCodeMapping"]}
                    />
                  </div>
                  <div className="w-52">
                    <Switch
                      checked={errorCodeMapping !== "null"}
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
                {errorCodeMapping !== "null" && (
                  <ErrorCodeMappingEditor
                    label={label}
                    value={errorCodeMapping}
                    onChange={(next) => update({ errorCodeMapping: next })}
                    readOnly={!editable}
                  />
                )}
              </div>
              <BlockExecutionOptions
                continueOnFailure={continueOnFailure}
                nextLoopOnFailure={nextLoopOnFailure}
                editable={editable}
                isInsideForLoop={isInsideForLoop}
                parentLoopSkipsOnFail={parentLoopSkipsOnFail}
                blockType="download"
                onContinueOnFailureChange={(checked) =>
                  update({ continueOnFailure: checked })
                }
                onNextLoopOnFailureChange={(checked) =>
                  update({ nextLoopOnFailure: checked })
                }
              />
              <DisableCache
                disableCache={disableCache}
                editable={editable}
                onDisableCacheChange={(next) => update({ disableCache: next })}
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
                  <Label className="text-xs font-normal text-slate-300">
                    File Name
                  </Label>
                  <HelpTooltip
                    content={helpTooltips["download"]["fileSuffix"]}
                  />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={blockId}
                  onChange={(next) => update({ downloadSuffix: next })}
                  value={downloadSuffix ?? ""}
                  placeholder={placeholders["download"]["downloadSuffix"]}
                  className="nopan text-xs"
                />
              </div>
              <Separator />
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">
                    2FA Identifier
                  </Label>
                  <HelpTooltip
                    content={helpTooltips["download"]["totpIdentifier"]}
                  />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={blockId}
                  onChange={(next) => update({ totpIdentifier: next })}
                  value={totpIdentifier ?? ""}
                  placeholder={
                    !totpIdentifier?.trim() && credentialTotpIdentifier
                      ? `${credentialTotpIdentifier} (from credential)`
                      : placeholders["download"]["totpIdentifier"]
                  }
                  className="nopan text-xs"
                />
                {!totpIdentifier?.trim() && credentialTotpIdentifier ? (
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
                  onChange={(next) => update({ totpVerificationUrl: next })}
                  value={totpVerificationUrl ?? ""}
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

export { FileDownloadEditor };
