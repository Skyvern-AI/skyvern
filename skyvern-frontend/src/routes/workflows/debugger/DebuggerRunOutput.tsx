import { FileIcon } from "@radix-ui/react-icons";
import { CodeEditor } from "../components/CodeEditor";
import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";
import { useActiveWorkflowRunItem } from "@/routes/workflows/workflowRun/useActiveWorkflowRunItem";
import {
  hasExtractedInformation,
  isAction,
  isWorkflowRunBlock,
} from "../types/workflowRunTypes";
import { findBlockSurroundingAction } from "@/routes/workflows/workflowRun/workflowTimelineUtils";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import { Status } from "@/api/types";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { isTaskVariantBlock } from "../types/workflowTypes";
import { statusIsAFailureType } from "@/routes/tasks/types";

function DebuggerRunOutput() {
  const { data: workflowRunTimeline, isLoading: workflowRunTimelineIsLoading } =
    useWorkflowRunTimelineQuery();
  const [activeItem] = useActiveWorkflowRunItem();
  const { data: workflowRun } = useWorkflowRunQuery();

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

  const showFailureReason =
    activeBlock &&
    activeBlock.status !== null &&
    (statusIsAFailureType({ status: activeBlock.status }) ||
      activeBlock.status === Status.Canceled);

  const outputs = workflowRun?.outputs;
  const fileUrls = workflowRun?.downloaded_file_urls ?? [];
  const observerOutput = workflowRun?.task_v2?.output;
  const webhookFailureReasonData =
    workflowRun?.task_v2?.webhook_failure_reason ??
    workflowRun?.webhook_failure_reason;

  function getFilenameFromUrl(url: string, index: number): string {
    try {
      const urlObj = new URL(url);
      const pathname = urlObj.pathname;
      const filename = pathname.split("/").pop() || "";
      if (filename && filename.includes(".")) {
        return decodeURIComponent(filename);
      }
    } catch {
      const parts = url.split("/");
      const lastPart = parts[parts.length - 1];
      if (lastPart && lastPart.includes(".")) {
        const filenamePart = lastPart.split("?")[0] || lastPart;
        try {
          return decodeURIComponent(filenamePart);
        } catch {
          return filenamePart;
        }
      }
    }
    return `File ${index + 1}`;
  }

  return (
    <div className="space-y-5">
      {webhookFailureReasonData ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-sm font-bold">Webhook Failure Reason</h1>
            <div className="space-y-2 text-yellow-600">
              {webhookFailureReasonData}
            </div>
          </div>
        </div>
      ) : null}
      {activeBlock ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-sm font-bold">Block Outputs</h1>
            {showFailureReason ? (
              <div className="space-y-2">
                <h2 className="text-sm">Failure Reason</h2>
                <AutoResizingTextarea
                  value={
                    activeBlock.status === "canceled"
                      ? "This block was cancelled"
                      : activeBlock.failure_reason ?? ""
                  }
                  readOnly
                />
              </div>
            ) : showExtractedInformation ? (
              <div className="space-y-2">
                <h2 className="text-sm">Extracted Information</h2>
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
              </div>
            ) : activeBlock.output !== null ? (
              <div className="space-y-2">
                <h2 className="text-sm">Output</h2>
                <CodeEditor
                  language="json"
                  value={JSON.stringify(activeBlock.output, null, 2)}
                  minHeight="96px"
                  maxHeight="200px"
                  readOnly
                />
              </div>
            ) : (
              <div className="text-sm">This block has no outputs</div>
            )}
          </div>
        </div>
      ) : null}
      {observerOutput ? (
        <div className="rounded bg-slate-elevation2 p-6">
          <div className="space-y-4">
            <h1 className="text-sm font-bold">Task 2.0 Output</h1>
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
          <h1 className="text-sm font-bold">Workflow Run Outputs</h1>
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
          <h1 className="text-sm font-bold">Workflow Run Downloaded Files</h1>
          <div className="space-y-2">
            {fileUrls.length > 0 ? (
              fileUrls.map((url, index) => {
                const filename = getFilenameFromUrl(url, index);
                return (
                  <div key={url} title={url} className="flex gap-2">
                    <FileIcon className="size-6" />
                    <a href={url} className="underline underline-offset-4">
                      <span>{filename}</span>
                    </a>
                  </div>
                );
              })
            ) : (
              <div className="text-sm">No files downloaded</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export { DebuggerRunOutput };
