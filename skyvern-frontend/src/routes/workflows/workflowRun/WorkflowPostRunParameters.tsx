import { useMemo } from "react";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { CodeEditor } from "../components/CodeEditor";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { useActiveWorkflowRunItem } from "./useActiveWorkflowRunItem";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import { isAction, isWorkflowRunBlock } from "../types/workflowRunTypes";
import { findBlockSurroundingAction } from "./workflowTimelineUtils";
import { TaskBlockParameters } from "./TaskBlockParameters";
import {
  isTaskVariantBlock,
  WorkflowBlockTypes,
  type WorkflowBlock,
  type WorkflowBlockType,
} from "../types/workflowTypes";
import { Input } from "@/components/ui/input";
import { ProxySelector } from "@/components/ProxySelector";
import { SendEmailBlockParameters } from "./blockInfo/SendEmailBlockInfo";
import { ProxyLocation } from "@/api/types";
import { KeyValueInput } from "@/components/KeyValueInput";
import { CodeBlockParameters } from "./blockInfo/CodeBlockParameters";
import { TextPromptBlockParameters } from "./blockInfo/TextPromptBlockParameters";
import { GotoUrlBlockParameters } from "./blockInfo/GotoUrlBlockParameters";
import { FileDownloadBlockParameters } from "./blockInfo/FileDownloadBlockParameters";

function WorkflowPostRunParameters() {
  const { data: workflowRunTimeline, isLoading: workflowRunTimelineIsLoading } =
    useWorkflowRunTimelineQuery();
  const [activeItem] = useActiveWorkflowRunItem();
  const { data: workflowRun, isLoading: workflowRunIsLoading } =
    useWorkflowRunWithWorkflowQuery();
  const parameters = workflowRun?.parameters ?? {};
  const workflow = workflowRun?.workflow;

  function getActiveBlock() {
    if (!workflowRunTimeline) {
      return;
    }
    if (isWorkflowRunBlock(activeItem)) {
      return activeItem;
    }
    if (isAction(activeItem)) {
      return findBlockSurroundingAction(
        workflowRunTimeline,
        activeItem.action_id,
      );
    }
  }

  const activeBlock = getActiveBlock();
  const activeBlockLabel = activeBlock?.label ?? null;
  const definitionBlock = useMemo(() => {
    if (!workflow || !activeBlockLabel) {
      return null;
    }
    return findWorkflowBlockByLabel(
      workflow.workflow_definition.blocks,
      activeBlockLabel,
    );
  }, [workflow, activeBlockLabel]);
  const isTaskV2 = Boolean(workflowRun?.task_v2);

  const webhookCallbackUrl = isTaskV2
    ? workflowRun?.task_v2?.webhook_callback_url ?? null
    : workflowRun?.webhook_callback_url ?? null;

  const proxyLocation = isTaskV2
    ? workflowRun?.task_v2?.proxy_location ?? null
    : workflowRun?.proxy_location ?? null;

  const extraHttpHeaders = isTaskV2
    ? workflowRun?.task_v2?.extra_http_headers ?? null
    : workflowRun?.extra_http_headers ?? null;

  if (workflowRunIsLoading || workflowRunTimelineIsLoading) {
    return <div>Loading workflow parameters...</div>;
  }

  if (!workflowRun || !workflowRunTimeline) {
    return null;
  }

  return (
    <div className="space-y-5">
      {activeBlock && isTaskVariantBlock(activeBlock) ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-lg font-bold">Block Parameters</h1>
            <TaskBlockParameters block={activeBlock} />
          </div>
        </div>
      ) : null}
      {activeBlock &&
      activeBlock.block_type === WorkflowBlockTypes.FileDownload &&
      isBlockOfType(definitionBlock, WorkflowBlockTypes.FileDownload) ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-lg font-bold">File Download Settings</h1>
            <FileDownloadBlockParameters
              prompt={
                activeBlock.navigation_goal ??
                definitionBlock.navigation_goal ??
                null
              }
              downloadSuffix={definitionBlock.download_suffix ?? null}
              downloadTimeout={definitionBlock.download_timeout ?? null}
              errorCodeMapping={definitionBlock.error_code_mapping ?? null}
              maxRetries={definitionBlock.max_retries ?? null}
              maxStepsPerRun={definitionBlock.max_steps_per_run ?? null}
            />
          </div>
        </div>
      ) : null}
      {activeBlock &&
      activeBlock.block_type === WorkflowBlockTypes.SendEmail ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-lg font-bold">Block Parameters</h1>
            <SendEmailBlockParameters
              body={activeBlock.body ?? ""}
              recipients={activeBlock.recipients ?? []}
              subject={activeBlock.subject ?? ""}
            />
          </div>
        </div>
      ) : null}
      {activeBlock && activeBlock.block_type === WorkflowBlockTypes.ForLoop ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-lg font-bold">Block Parameters</h1>
            <div className="flex gap-16">
              <div className="w-80">
                <h1 className="text-lg">Loop Values</h1>
                <h2 className="text-base text-slate-400">
                  The values that are being looped over
                </h2>
              </div>
              <CodeEditor
                className="w-full"
                language="json"
                value={JSON.stringify(activeBlock.loop_values, null, 2)}
                readOnly
                minHeight="96px"
                maxHeight="200px"
              />
            </div>
          </div>
        </div>
      ) : null}
      {activeBlock &&
      activeBlock.block_type === WorkflowBlockTypes.Code &&
      isBlockOfType(definitionBlock, WorkflowBlockTypes.Code) ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-lg font-bold">Code Block</h1>
            <CodeBlockParameters
              code={definitionBlock.code}
              parameters={definitionBlock.parameters}
            />
          </div>
        </div>
      ) : null}
      {activeBlock &&
      activeBlock.block_type === WorkflowBlockTypes.TextPrompt &&
      isBlockOfType(definitionBlock, WorkflowBlockTypes.TextPrompt) ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-lg font-bold">Text Prompt Block</h1>
            <TextPromptBlockParameters
              prompt={activeBlock.prompt ?? definitionBlock.prompt ?? ""}
              llmKey={definitionBlock.llm_key}
              jsonSchema={definitionBlock.json_schema}
              parameters={definitionBlock.parameters}
            />
          </div>
        </div>
      ) : null}
      {activeBlock && activeBlock.block_type === WorkflowBlockTypes.URL ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-lg font-bold">Go To URL Block</h1>
            <GotoUrlBlockParameters
              url={
                activeBlock.url ??
                (isBlockOfType(definitionBlock, WorkflowBlockTypes.URL)
                  ? definitionBlock.url
                  : "")
              }
              continueOnFailure={activeBlock.continue_on_failure}
            />
          </div>
        </div>
      ) : null}
      <div className="rounded bg-slate-elevation2 p-6">
        <div className="space-y-4">
          <h1 className="text-lg font-bold">Workflow Input Parameters</h1>
          {Object.entries(parameters).map(([key, value]) => {
            return (
              <div key={key} className="flex gap-16">
                <span className="w-80 truncate text-lg" title={key}>
                  {key}
                </span>
                {typeof value === "string" ||
                typeof value === "number" ||
                typeof value === "boolean" ? (
                  <AutoResizingTextarea value={String(value)} readOnly />
                ) : (
                  <CodeEditor
                    value={JSON.stringify(value, null, 2)}
                    readOnly
                    language="json"
                    minHeight="96px"
                    maxHeight="200px"
                    className="w-full"
                  />
                )}
              </div>
            );
          })}
          {Object.entries(parameters).length === 0 ? (
            <div>No input parameters found for this workflow</div>
          ) : null}
          <h1 className="text-lg font-bold">Other Workflow Parameters</h1>
          <div className="flex gap-16">
            <div className="w-80">
              <h1 className="text-lg">Webhook Callback URL</h1>
            </div>
            <Input value={webhookCallbackUrl ?? ""} readOnly />
          </div>
          <div className="flex gap-16">
            <div className="w-80">
              <h1 className="text-lg">Proxy Location</h1>
            </div>
            <ProxySelector
              value={proxyLocation ?? ProxyLocation.Residential}
              onChange={() => {
                // TODO
              }}
            />
          </div>
          <div className="flex gap-16">
            <div className="w-80">
              <h1 className="text-lg">Extra HTTP Headers</h1>
            </div>
            <div className="w-full">
              <KeyValueInput
                value={
                  extraHttpHeaders ? JSON.stringify(extraHttpHeaders) : null
                }
                readOnly={true}
                onChange={() => {}}
              />
            </div>
          </div>
        </div>
      </div>
      {workflowRun.task_v2 ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-lg font-bold">Task 2.0 Parameters</h1>
            <div className="flex gap-16">
              <div className="w-80">
                <h1 className="text-lg">Task 2.0 Prompt</h1>
                <h2 className="text-base text-slate-400">
                  The original prompt for the task
                </h2>
              </div>
              <AutoResizingTextarea
                value={workflowRun.task_v2.prompt ?? ""}
                readOnly
              />
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export { WorkflowPostRunParameters };

function findWorkflowBlockByLabel(
  blocks: Array<WorkflowBlock>,
  label: string,
): WorkflowBlock | null {
  for (const block of blocks) {
    if (block.label === label) {
      return block;
    }
    if (
      block.block_type === WorkflowBlockTypes.ForLoop &&
      block.loop_blocks.length > 0
    ) {
      const nested = findWorkflowBlockByLabel(block.loop_blocks, label);
      if (nested) {
        return nested;
      }
    }
  }
  return null;
}

function isBlockOfType<T extends WorkflowBlockType>(
  block: WorkflowBlock | null,
  type: T,
): block is Extract<WorkflowBlock, { block_type: T }> {
  return block?.block_type === type;
}
