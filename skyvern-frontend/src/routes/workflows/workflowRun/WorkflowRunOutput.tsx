import { FileIcon } from "@radix-ui/react-icons";
import { CodeEditor } from "../components/CodeEditor";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { useActiveWorkflowRunItem } from "./useActiveWorkflowRunItem";
import {
  hasExtractedInformation,
  isAction,
  isWorkflowRunBlock,
} from "../types/workflowRunTypes";
import { findBlockSurroundingAction } from "./workflowTimelineUtils";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import { Status } from "@/api/types";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { isTaskVariantBlock } from "../types/workflowTypes";

function WorkflowRunOutput() {
  const { data: workflowRunTimeline, isLoading: workflowRunTimelineIsLoading } =
    useWorkflowRunTimelineQuery();
  const [activeItem] = useActiveWorkflowRunItem();
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery();

  if (workflowRunTimelineIsLoading) {
    return <div>Loading...</div>;
  }

  if (!workflowRunTimeline) {
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

  const showExtractedInformation =
    activeBlock &&
    isTaskVariantBlock(activeBlock) &&
    activeBlock.status === Status.Completed;

  const outputs = workflowRun?.outputs;
  const fileUrls = workflowRun?.downloaded_file_urls ?? [];
  const observerOutput = workflowRun?.task_v2?.output;
  const webhookFailureReasonData =
    workflowRun?.task_v2?.webhook_failure_reason ??
    workflowRun?.webhook_failure_reason;

  return (
    <div className="space-y-5">
      {webhookFailureReasonData ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-lg font-bold">Webhook Failure Reason</h1>
            <div className="space-y-2 text-yellow-600">
              {webhookFailureReasonData}
            </div>
          </div>
        </div>
      ) : null}
      {activeBlock ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-lg font-bold">Block Outputs</h1>
            {activeBlock.output === null ? (
              <div>This block has no outputs</div>
            ) : isTaskVariantBlock(activeBlock) ? (
              <div className="space-y-2">
                <h2>
                  {showExtractedInformation
                    ? "Extracted Information"
                    : "Failure Reason"}
                </h2>
                {showExtractedInformation ? (
                  <CodeEditor
                    language="json"
                    value={JSON.stringify(
                      (hasExtractedInformation(activeBlock.output) &&
                        activeBlock.output.extracted_information) ??
                        null,
                      null,
                      2,
                    )}
                    minHeight="96px"
                    maxHeight="200px"
                    readOnly
                  />
                ) : (
                  <AutoResizingTextarea
                    value={
                      activeBlock.status === "canceled"
                        ? "This block was cancelled"
                        : activeBlock.failure_reason ?? ""
                    }
                    readOnly
                  />
                )}
              </div>
            ) : (
              <div className="space-y-2">
                <h2>Output</h2>
                <CodeEditor
                  language="json"
                  value={JSON.stringify(activeBlock.output, null, 2)}
                  minHeight="96px"
                  maxHeight="200px"
                  readOnly
                />
              </div>
            )}
          </div>
        </div>
      ) : null}
      {observerOutput ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-lg font-bold">Task 2.0 Output</h1>
            <CodeEditor
              language="json"
              value={JSON.stringify(observerOutput, null, 2)}
              readOnly
              minHeight="96px"
              maxHeight="200px"
            />
          </div>
        </div>
      ) : null}
      <div className="rounded bg-slate-elevation2 p-6">
        <div className="space-y-4">
          <h1 className="text-lg font-bold">Workflow Run Outputs</h1>
          <CodeEditor
            language="json"
            value={outputs ? JSON.stringify(outputs, null, 2) : ""}
            readOnly
            minHeight="96px"
            maxHeight="200px"
          />
        </div>
      </div>
      <div className="rounded bg-slate-elevation2 p-6">
        <div className="space-y-4">
          <h1 className="text-lg font-bold">Workflow Run Downloaded Files</h1>
          <div className="space-y-2">
            {fileUrls.length > 0 ? (
              fileUrls.map((url, index) => {
                return (
                  <div key={url} title={url} className="flex gap-2">
                    <FileIcon className="size-6" />
                    <a href={url} className="underline underline-offset-4">
                      <span>{`File ${index + 1}`}</span>
                    </a>
                  </div>
                );
              })
            ) : (
              <div>No files downloaded</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export { WorkflowRunOutput };
