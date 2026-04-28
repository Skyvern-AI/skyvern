import { FileIcon } from "@radix-ui/react-icons";
import { useEffect, useMemo, useRef, useState } from "react";
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
import { SummarizeOutput } from "@/components/SummarizeOutput";
import { isTaskVariantBlock } from "../types/workflowTypes";
import { statusIsAFailureType } from "@/routes/tasks/types";
import {
  filenameForDownloadedFileUrl,
  getBlockDownloadedFileUrls,
} from "./blockDownloadedFiles";

function SummaryDisplay({
  summary,
  isStale,
}: {
  summary: string;
  isStale: boolean;
}) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="space-y-1 rounded bg-slate-elevation3 p-4"
    >
      {isStale && (
        <p className="text-xs text-slate-400">
          Out of date - re-summarize for the current output.
        </p>
      )}
      <p
        className={
          isStale
            ? "whitespace-pre-wrap text-sm text-slate-400"
            : "whitespace-pre-wrap text-sm"
        }
      >
        {summary}
      </p>
    </div>
  );
}

function WorkflowRunOutput() {
  const [searchParams] = useSearchParams();
  const hasExplicitSelection = searchParams.has("active");
  const { data: workflowRunTimeline, isLoading: workflowRunTimelineIsLoading } =
    useWorkflowRunTimelineQuery();
  const [activeItem] = useActiveWorkflowRunItem();
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery();
  const [blockSummaries, setBlockSummaries] = useState<
    Record<string, { summary: string; identity: string }>
  >({});
  const [outputSummary, setOutputSummary] = useState<{
    summary: string;
    identity: string;
  } | null>(null);

  const currentWorkflowRunId = workflowRun?.workflow_run_id;
  const prevRunIdRef = useRef(currentWorkflowRunId);
  useEffect(() => {
    const prev = prevRunIdRef.current;
    prevRunIdRef.current = currentWorkflowRunId;
    if (prev && prev !== currentWorkflowRunId) {
      setBlockSummaries({});
      setOutputSummary(null);
    }
  }, [currentWorkflowRunId]);

  // Local override so users on finalized runs (where there is no neutral
  // timeline selection to return to) can still view the full workflow-level
  // file list without clearing their block selection elsewhere on the page.
  const [showAllFilesOverride, setShowAllFilesOverride] = useState(false);

  const activeBlock = useMemo(() => {
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
  }, [workflowRunTimeline, activeItem]);
  const activeBlockId = activeBlock?.workflow_run_block_id;
  useEffect(() => {
    setShowAllFilesOverride(false);
  }, [activeBlockId]);

  const outputs = workflowRun?.outputs;

  const workflowOutputJsonCompact = useMemo(
    () => (outputs ? JSON.stringify(outputs) : ""),
    [outputs],
  );

  const workflowOutputJson = useMemo(
    () => (outputs ? JSON.stringify(outputs, null, 2) : ""),
    [outputs],
  );

  const activeBlockOutput = activeBlock?.output;
  const activeBlockStatus = activeBlock?.status;
  const activeBlockType = activeBlock?.block_type;
  const blockOutputJsonCompact = useMemo(() => {
    if (activeBlockOutput === undefined || activeBlockOutput === null) {
      return "";
    }
    const isCompletedTaskBlock =
      activeBlockType !== undefined &&
      isTaskVariantBlock({ block_type: activeBlockType }) &&
      activeBlockStatus === Status.Completed;
    const value =
      isCompletedTaskBlock && hasExtractedInformation(activeBlockOutput)
        ? activeBlockOutput.extracted_information
        : activeBlockOutput;
    return JSON.stringify(value);
  }, [activeBlockOutput, activeBlockStatus, activeBlockType]);

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

  const blockContextKey = activeBlock
    ? `block:${activeBlock.workflow_run_id}:${activeBlock.workflow_run_block_id}`
    : "";
  const blockIdentity = `${blockContextKey}|${blockOutputJsonCompact}`;
  const storedBlockSummary = activeBlock
    ? blockSummaries[activeBlock.workflow_run_block_id]
    : undefined;
  const blockSummaryIsStale =
    !!storedBlockSummary && storedBlockSummary.identity !== blockIdentity;

  const runContextKey = `run:${currentWorkflowRunId ?? ""}`;
  const runIdentity = `${runContextKey}|${workflowOutputJsonCompact}`;
  const outputSummaryIsStale =
    !!outputSummary && outputSummary.identity !== runIdentity;

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
            <div className="flex items-center justify-between">
              <h1 className="text-lg font-bold">Block Outputs</h1>
              {activeBlock.output !== null && !showFailureReason && (
                <SummarizeOutput
                  key={blockContextKey}
                  contextKey={blockContextKey}
                  outputJson={blockOutputJsonCompact}
                  workflowTitle={workflowRun?.workflow_title}
                  blockLabel={activeBlock.label}
                  hasSummary={!!storedBlockSummary}
                  onSummary={(summary) =>
                    setBlockSummaries((prev) => ({
                      ...prev,
                      [activeBlock.workflow_run_block_id]: {
                        summary,
                        identity: blockIdentity,
                      },
                    }))
                  }
                />
              )}
            </div>
            {storedBlockSummary && (
              <SummaryDisplay
                summary={storedBlockSummary.summary}
                isStale={blockSummaryIsStale}
              />
            )}
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
          <div className="flex items-center justify-between">
            <h1 className="text-lg font-bold">Workflow Run Outputs</h1>
            {outputs && (
              <SummarizeOutput
                key={runContextKey}
                contextKey={runContextKey}
                outputJson={workflowOutputJsonCompact}
                workflowTitle={workflowRun?.workflow_title}
                hasSummary={!!outputSummary}
                onSummary={(summary) =>
                  setOutputSummary({
                    summary,
                    identity: runIdentity,
                  })
                }
              />
            )}
          </div>
          {outputSummary && (
            <SummaryDisplay
              summary={outputSummary.summary}
              isStale={outputSummaryIsStale}
            />
          )}
          <CodeEditor
            language="json"
            value={workflowOutputJson}
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
                const filename = filenameForDownloadedFileUrl(url);
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
