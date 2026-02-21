import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";
import { CodeEditor } from "../components/CodeEditor";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { useActiveWorkflowRunItem } from "@/routes/workflows/workflowRun/useActiveWorkflowRunItem";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import { isAction, isWorkflowRunBlock } from "../types/workflowRunTypes";
import { findBlockSurroundingAction } from "@/routes/workflows/workflowRun/workflowTimelineUtils";
import { DebuggerTaskBlockParameters } from "./DebuggerTaskBlockParameters";
import { isTaskVariantBlock, WorkflowBlockTypes } from "../types/workflowTypes";
import { Input } from "@/components/ui/input";
import { ProxySelector } from "@/components/ProxySelector";
import { DebuggerSendEmailBlockParameters } from "./DebuggerSendEmailBlockInfo";
import { ProxyLocation } from "@/api/types";
import { KeyValueInput } from "@/components/KeyValueInput";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Switch } from "@/components/ui/switch";

function DebuggerPostRunParameters() {
  const { data: workflowRunTimeline, isLoading: workflowRunTimelineIsLoading } =
    useWorkflowRunTimelineQuery();
  const [activeItem] = useActiveWorkflowRunItem();
  const { data: workflowRun, isLoading: workflowRunIsLoading } =
    useWorkflowRunQuery();
  const parameters = workflowRun?.parameters ?? {};

  if (workflowRunIsLoading || workflowRunTimelineIsLoading) {
    return <div>Loading workflow parameters...</div>;
  }

  if (!workflowRun || !workflowRunTimeline) {
    return null;
  }

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
  const isTaskV2 = workflowRun.task_v2 !== null;

  const webhookCallbackUrl = isTaskV2
    ? workflowRun.task_v2?.webhook_callback_url
    : workflowRun.webhook_callback_url;

  const proxyLocation = isTaskV2
    ? workflowRun.task_v2?.proxy_location
    : workflowRun.proxy_location;

  const extraHttpHeaders = isTaskV2
    ? workflowRun.task_v2?.extra_http_headers
    : workflowRun.extra_http_headers;

  return (
    <div className="space-y-5">
      {activeBlock && isTaskVariantBlock(activeBlock) ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-sm font-bold">Task Block Parameters</h1>
            <DebuggerTaskBlockParameters block={activeBlock} />
          </div>
        </div>
      ) : null}
      {activeBlock &&
      activeBlock.block_type === WorkflowBlockTypes.SendEmail ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-sm font-bold">Email Block Parameters</h1>
            <DebuggerSendEmailBlockParameters
              body={activeBlock?.body ?? ""}
              recipients={activeBlock?.recipients ?? []}
              subject={activeBlock?.subject ?? ""}
            />
          </div>
        </div>
      ) : null}
      {activeBlock && activeBlock.block_type === WorkflowBlockTypes.ForLoop ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-sm font-bold">For Loop Block Parameters</h1>
            <div className="flex flex-col gap-2">
              <div className="flex w-full items-center justify-start gap-2">
                <h1 className="text-sm">Loop Values</h1>
                <HelpTooltip content="The values that are being looped over." />
              </div>
              <CodeEditor
                className="w-full"
                language="json"
                value={JSON.stringify(activeBlock?.loop_values, null, 2)}
                readOnly
                minHeight="96px"
                maxHeight="200px"
              />
            </div>
          </div>
        </div>
      ) : null}
      {activeBlock && activeBlock.block_type === WorkflowBlockTypes.Wait ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-sm font-bold">Wait Block Parameters</h1>
            <div className="flex flex-col gap-2">
              <div className="flex w-full items-center justify-start gap-2">
                <h1 className="text-sm">Wait Duration</h1>
                <HelpTooltip content="Seconds to wait before proceeding." />
              </div>
              <Input
                value={
                  typeof activeBlock.wait_sec === "number"
                    ? `${activeBlock.wait_sec}s`
                    : "N/A"
                }
                readOnly
              />
            </div>
          </div>
        </div>
      ) : null}
      {activeBlock &&
      activeBlock.block_type === WorkflowBlockTypes.HumanInteraction ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-sm font-bold">
              Human Interaction Block Parameters
            </h1>
            {activeBlock.instructions ? (
              <div className="flex flex-col gap-2">
                <div className="flex w-full items-center justify-start gap-2">
                  <h1 className="text-sm">Instructions</h1>
                  <HelpTooltip content="Instructions for the human interaction." />
                </div>
                <AutoResizingTextarea
                  value={activeBlock.instructions}
                  readOnly
                />
              </div>
            ) : null}
            {activeBlock.positive_descriptor ? (
              <div className="flex flex-col gap-2">
                <h1 className="text-sm">Positive Descriptor</h1>
                <Input value={activeBlock.positive_descriptor} readOnly />
              </div>
            ) : null}
            {activeBlock.negative_descriptor ? (
              <div className="flex flex-col gap-2">
                <h1 className="text-sm">Negative Descriptor</h1>
                <Input value={activeBlock.negative_descriptor} readOnly />
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
      {activeBlock &&
      activeBlock.block_type === WorkflowBlockTypes.Conditional ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-sm font-bold">Conditional Block Parameters</h1>
            {activeBlock.executed_branch_expression ? (
              <div className="flex flex-col gap-2">
                <div className="flex w-full items-center justify-start gap-2">
                  <h1 className="text-sm">Executed Expression</h1>
                  <HelpTooltip content="The branch expression that was evaluated." />
                </div>
                <AutoResizingTextarea
                  value={activeBlock.executed_branch_expression}
                  readOnly
                />
              </div>
            ) : null}
            {typeof activeBlock.executed_branch_result === "boolean" ? (
              <div className="flex flex-col gap-2">
                <h1 className="text-sm">Branch Result</h1>
                <div className="flex items-center gap-3">
                  <Switch
                    checked={activeBlock.executed_branch_result}
                    disabled
                  />
                  <span className="text-sm text-slate-400">
                    {activeBlock.executed_branch_result ? "True" : "False"}
                  </span>
                </div>
              </div>
            ) : null}
            {activeBlock.executed_branch_next_block ? (
              <div className="flex flex-col gap-2">
                <h1 className="text-sm">Next Block</h1>
                <Input
                  value={activeBlock.executed_branch_next_block}
                  readOnly
                />
              </div>
            ) : null}
            {activeBlock.executed_branch_id ? (
              <div className="flex flex-col gap-2">
                <h1 className="text-sm">Executed Branch ID</h1>
                <Input value={activeBlock.executed_branch_id} readOnly />
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
      {activeBlock &&
      activeBlock.block_type === WorkflowBlockTypes.TextPrompt ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-sm font-bold">Text Prompt Block Parameters</h1>
            {activeBlock.prompt ? (
              <div className="flex flex-col gap-2">
                <div className="flex w-full items-center justify-start gap-2">
                  <h1 className="text-sm">Prompt</h1>
                  <HelpTooltip content="Instructions passed to the selected LLM." />
                </div>
                <AutoResizingTextarea value={activeBlock.prompt} readOnly />
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
      <div className="rounded bg-slate-elevation2 p-6">
        <div className="space-y-4">
          <h1 className="text-sm font-bold">Workflow Parameters</h1>
          {Object.entries(parameters).map(([key, value]) => {
            return (
              <div key={key} className="flex flex-col gap-2">
                <div className="flex w-full items-center justify-start gap-2">
                  <h1 className="text-sm">{key}</h1>
                  <HelpTooltip content="The value of the parameter." />
                </div>
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
            <div className="text-sm">
              No input parameters found for this workflow
            </div>
          ) : null}
          <h1 className="text-sm font-bold">Other Workflow Parameters</h1>
          <div className="flex flex-col gap-2">
            <div className="flex w-full items-center justify-start gap-2">
              <h1 className="text-sm">Webhook Callback URL</h1>
              <HelpTooltip content="The webhook callback URL for the workflow." />
            </div>
            <Input value={webhookCallbackUrl ?? ""} readOnly />
          </div>
          <div className="flex flex-col gap-2">
            <div className="flex w-full items-center justify-start gap-2">
              <h1 className="text-sm">Proxy Location</h1>
              <HelpTooltip content="The proxy location for the workflow." />
            </div>
            <ProxySelector
              value={proxyLocation ?? ProxyLocation.Residential}
              onChange={() => {
                // TODO
              }}
            />
          </div>
          <div className="flex flex-col gap-2">
            <div className="flex w-full items-center justify-start gap-2">
              <h1 className="text-sm">Extra HTTP Headers</h1>
              <HelpTooltip content="The extra HTTP headers for the workflow." />
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
          {workflowRun.browser_session_id ? (
            <div className="flex flex-col gap-2">
              <div className="flex w-full items-center justify-start gap-2">
                <h1 className="text-sm">Browser Session ID</h1>
                <HelpTooltip content="The browser session ID used for this run." />
              </div>
              <Input value={workflowRun.browser_session_id} readOnly />
            </div>
          ) : null}
          {workflowRun.max_screenshot_scrolls != null ? (
            <div className="flex flex-col gap-2">
              <div className="flex w-full items-center justify-start gap-2">
                <h1 className="text-sm">Max Screenshot Scrolls</h1>
                <HelpTooltip content="Maximum number of screenshot scrolls." />
              </div>
              <Input
                value={workflowRun.max_screenshot_scrolls.toString()}
                readOnly
              />
            </div>
          ) : null}
        </div>
      </div>
      {workflowRun.task_v2 ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-sm font-bold">Task 2.0 Parameters</h1>
            <div className="flex flex-col gap-2">
              <div className="flex w-full items-center justify-start gap-2">
                <h1 className="text-sm">Task 2.0 Prompt</h1>
                <HelpTooltip content="The original prompt for the task." />
              </div>
              <AutoResizingTextarea
                value={workflowRun.task_v2?.prompt ?? ""}
                readOnly
              />
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export { DebuggerPostRunParameters };
