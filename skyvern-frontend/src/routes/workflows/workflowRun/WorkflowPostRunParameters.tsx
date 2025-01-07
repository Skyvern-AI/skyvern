import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";
import { CodeEditor } from "../components/CodeEditor";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { useActiveWorkflowRunItem } from "./useActiveWorkflowRunItem";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import { isAction, isWorkflowRunBlock } from "../types/workflowRunTypes";
import { findBlockSurroundingAction } from "./workflowTimelineUtils";
import { TaskBlockParameters } from "./TaskBlockParameters";
import { isTaskVariantBlock, WorkflowBlockTypes } from "../types/workflowTypes";
import { Input } from "@/components/ui/input";
import { ProxySelector } from "@/components/ProxySelector";
import { SendEmailBlockParameters } from "./blockInfo/SendEmailBlockInfo";
import { ProxyLocation } from "@/api/types";

function WorkflowPostRunParameters() {
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
      <div className="rounded bg-slate-elevation2 p-6">
        <div className="space-y-4">
          <h1 className="text-lg font-bold">Workflow Input Parameters</h1>
          {Object.entries(parameters).map(([key, value]) => {
            return (
              <div key={key} className="flex gap-16">
                <div className="w-80">
                  <h1 className="text-lg">{key}</h1>
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
            <div>No input parameters found for this workflow</div>
          ) : null}
          <h1 className="text-lg font-bold">Other Workflow Parameters</h1>
          <div className="flex gap-16">
            <div className="w-80">
              <h1 className="text-lg">Webhook Callback URL</h1>
            </div>
            <Input value={workflowRun.webhook_callback_url ?? ""} readOnly />
          </div>
          <div className="flex gap-16">
            <div className="w-80">
              <h1 className="text-lg">Proxy Location</h1>
            </div>
            <ProxySelector
              value={workflowRun.proxy_location ?? ProxyLocation.Residential}
              onChange={() => {
                // TODO
              }}
            />
          </div>
        </div>
      </div>
      {workflowRun.observer_cruise ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-lg font-bold">Observer Parameters</h1>
            <div className="flex gap-16">
              <div className="w-80">
                <h1 className="text-lg">Observer Prompt</h1>
                <h2 className="text-base text-slate-400">
                  The original prompt for the observer
                </h2>
              </div>
              <AutoResizingTextarea
                value={workflowRun.observer_cruise.prompt ?? ""}
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
