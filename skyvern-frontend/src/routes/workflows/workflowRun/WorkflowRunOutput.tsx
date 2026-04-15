import { FileIcon } from "@radix-ui/react-icons";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
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
import { statusIsAFailureType } from "@/routes/tasks/types";
import { getBlockDownloadedFileUrls } from "./blockDownloadedFiles";

function WorkflowRunOutput() {
  const [searchParams] = useSearchParams();
  const hasExplicitSelection = searchParams.has("active");
  const { data: workflowRunTimeline, isLoading: workflowRunTimelineIsLoading } =
    useWorkflowRunTimelineQuery();
  const [activeItem] = useActiveWorkflowRunItem();
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery();

  // Local override so users on finalized runs (where there is no neutral
  // timeline selection to return to) can still view the full workflow-level
  // file list without clearing their block selection elsewhere on the page.
  const [showAllFilesOverride, setShowAllFilesOverride] = useState(false);

  const activeBlock = (() => {
    if (!workflowRunTimeline) {
      return undefined;
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
    return undefined;
  })();
  const activeBlockId = activeBlock?.workflow_run_block_id;
  useEffect(() => {
    setShowAllFilesOverride(false);
  }, [activeBlockId]);

  if (workflowRunTimelineIsLoading) {
    return <div>Loading...</div>;
  }

  if (!workflowRunTimeline) {
    return null;
  }

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
  const allFileUrls = workflowRun?.downloaded_file_urls ?? [];

  // Scope to the surrounding block whenever the user explicitly selected a
  // block or any action inside it, so drilling from a block into its child
  // steps keeps the files section anchored to that block. The helper swaps
  // expired block URLs for matching fresh run-level ones when available.
  const userSelectedBlock = Boolean(hasExplicitSelection && activeBlock);
  const showBlockFiles = userSelectedBlock && !showAllFilesOverride;
  const fileUrls =
    showBlockFiles && activeBlock
      ? getBlockDownloadedFileUrls(activeBlock.output, allFileUrls)
      : allFileUrls;
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
            {showFailureReason ? (
              <div className="space-y-2">
                <h2>Failure Reason</h2>
                <AutoResizingTextarea
                  value={
                    activeBlock.status === "canceled"
                      ? "This block was cancelled"
                      : (activeBlock.failure_reason ?? "")
                  }
                  readOnly
                />
              </div>
            ) : showExtractedInformation ? (
              <div className="space-y-2">
                <h2>Extracted Information</h2>
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
                <h2>Output</h2>
                <CodeEditor
                  language="json"
                  value={JSON.stringify(activeBlock.output, null, 2)}
                  minHeight="96px"
                  maxHeight="200px"
                  readOnly
                />
              </div>
            ) : (
              <div>This block has no outputs</div>
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
          <div className="flex items-center justify-between">
            <h1 className="text-lg font-bold">
              {showBlockFiles
                ? "Block Downloaded Files"
                : "Workflow Run Downloaded Files"}
            </h1>
            {userSelectedBlock ? (
              <Button
                variant="ghost"
                size="sm"
                className="h-7 px-2.5 text-xs"
                onClick={() => setShowAllFilesOverride((v) => !v)}
              >
                {showAllFilesOverride
                  ? "Show block files"
                  : "Show all workflow files"}
              </Button>
            ) : null}
          </div>
          <div className="space-y-2">
            {fileUrls.length > 0 ? (
              fileUrls.map((url) => {
                // Extract filename from URL path, stripping query params from signed URLs
                const urlPath = url.split("?")[0] ?? url;
                const filename = urlPath.split("/").pop() || "download";
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
              <div>No files downloaded</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export { WorkflowRunOutput };
