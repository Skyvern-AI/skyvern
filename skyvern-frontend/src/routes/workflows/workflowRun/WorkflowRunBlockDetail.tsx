import type { ActionsApiResponse } from "@/api/types";
import { useMemo } from "react";
import { FileIcon } from "@radix-ui/react-icons";
import { ArtifactDownloadLink } from "@/components/ArtifactDownloadLink";
import { statusIsFinalized } from "@/routes/tasks/types";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import {
  isAction,
  isObserverThought,
  isWorkflowRunBlock,
  type ObserverThought,
  type WorkflowRunBlock,
  type WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import {
  getBlockDownloadedFileUrls,
  pickDownloadedFileFilename,
} from "./blockDownloadedFiles";
import type { WorkflowRunOverviewActiveElement } from "./WorkflowRunOverview";
import {
  findBlockSurroundingAction,
  findBlockSurroundingThought,
  findLastExecutedBlock,
  findRunningBlock,
  findThoughtsForBlock,
} from "./workflowTimelineUtils";
import { BlockDetailConditional } from "./blockDetail/BlockDetailConditional";
import { BlockDetailGeneric } from "./blockDetail/BlockDetailGeneric";
import { BlockDetailHttpRequest } from "./blockDetail/BlockDetailHttpRequest";
import { BlockDetailHumanInteraction } from "./blockDetail/BlockDetailHumanInteraction";
import { BlockDetailTask } from "./blockDetail/BlockDetailTask";
import { BlockDetailThought } from "./blockDetail/BlockDetailThought";
import { BlockDetailWorkflowTrigger } from "./blockDetail/BlockDetailWorkflowTrigger";
import { BlockInspector } from "./blockDetail/BlockInspector";
import { EmptyState } from "./blockDetail/EmptyState";
import { BlockHealPanel } from "./BlockHealPanel";
import {
  BlockDetailHeader,
  BlockDetailHeaderSkeleton,
} from "./blockDetail/shared";

type Props = {
  activeItem: WorkflowRunOverviewActiveElement;
  activeIteration?: number | null;
  timeline: Array<WorkflowRunTimelineItem>;
  timelineReady?: boolean;
  showDownloadedFiles?: boolean;
  workflowRunId: string | undefined;
  onThoughtSelect?: (thought: ObserverThought) => void;
  onViewScreenshot?: (workflowRunBlockId: string) => void;
  // The block the run-level line's headline is actually about, and that
  // headline's exact text. Both must match — the ID alone (a block can fail
  // with different text than the run's own failure_reason) and the text
  // alone (an unrelated block, e.g. an earlier continue_on_failure failure,
  // can coincidentally parse to the same headline) are each insufficient on
  // their own to say "the line already stated this."
  statedFailureBlockId?: string | null;
  statedFailureHeadline?: string | null;
};

function BlockDownloadedFiles({
  block,
  workflowRunId,
}: {
  block: WorkflowRunBlock;
  workflowRunId: string | undefined;
}) {
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery({
    workflowRunId,
  });
  const files = useMemo(() => {
    const freshUrls = workflowRun?.downloaded_file_urls ?? [];
    const urls = getBlockDownloadedFileUrls(block.output, freshUrls);
    if (urls.length === 0) {
      return [];
    }
    const filenameByUrl = new Map<string, string>();
    for (const file of workflowRun?.downloaded_files ?? []) {
      if (file.filename) {
        filenameByUrl.set(file.url, file.filename);
      }
    }
    return urls.map((url) => ({
      url,
      filename: pickDownloadedFileFilename(url, filenameByUrl),
    }));
  }, [block.output, workflowRun]);

  if (files.length === 0) {
    return null;
  }

  return (
    <div className="border-b border-border bg-slate-elevation1 px-3 py-3">
      <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground dark:text-slate-500">
        Downloaded files
      </div>
      <div className="flex flex-col gap-2">
        {files.map((file) => (
          <div
            key={file.url}
            title={file.url}
            className="flex items-center gap-2 text-sm"
          >
            <FileIcon className="size-4 shrink-0 text-muted-foreground" />
            <ArtifactDownloadLink
              href={file.url}
              className="truncate underline underline-offset-4"
            >
              {file.filename}
            </ArtifactDownloadLink>
          </div>
        ))}
      </div>
    </div>
  );
}

function renderBodyForBlock(
  block: WorkflowRunBlock,
  activeItem: WorkflowRunOverviewActiveElement,
  onThoughtSelect: Props["onThoughtSelect"],
  timeline: Array<WorkflowRunTimelineItem>,
) {
  const thoughts = findThoughtsForBlock(timeline, block);
  switch (block.block_type) {
    case "task":
    case "task_v2":
    case "action":
    case "navigation":
    case "login":
    case "validation":
    case "extraction":
    case "file_download":
      return (
        <BlockDetailTask
          block={block}
          activeItem={activeItem}
          onThoughtSelect={onThoughtSelect}
          thoughts={thoughts}
        />
      );
    case "conditional":
      return <BlockDetailConditional block={block} />;
    case "http_request":
      return <BlockDetailHttpRequest block={block} />;
    case "workflow_trigger":
      return <BlockDetailWorkflowTrigger block={block} />;
    case "human_interaction":
      return <BlockDetailHumanInteraction block={block} />;
    default:
      return <BlockDetailGeneric block={block} />;
  }
}

function WorkflowRunBlockDetail({
  activeItem,
  activeIteration = null,
  timeline,
  timelineReady = true,
  showDownloadedFiles = false,
  workflowRunId,
  onThoughtSelect,
  onViewScreenshot,
  statedFailureBlockId = null,
  statedFailureHeadline = null,
}: Props) {
  // activeIteration is a URL hint scoped to a specific selection. In
  // fallback mode (null or "stream") the resolved block may not be the
  // loop the iteration was set for — ignore it to avoid stale labels.
  const effectiveIteration =
    activeItem === null || activeItem === "stream" ? null : activeIteration;
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery({
    workflowRunId,
  });

  // Cold-start: timeline data hasn't arrived yet. Check data === undefined
  // rather than isLoading because the timeline query is gated on the
  // workflowPermanentId (resolved by useWorkflowRunWithWorkflowQuery), so
  // during the workflow-run fetch the timeline query is `enabled: false`
  // and isLoading reports false even though there's no data to render.
  if (!timelineReady) {
    return (
      <>
        <div>
          <BlockDetailHeaderSkeleton />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
          <div />
        </div>
      </>
    );
  }

  // Resolve which block (if any) the active selection actually points at and
  // produce the matching body. Thoughts and the empty state are special:
  // they bypass the block header and render only as the body slot.
  let resolvedBlock: WorkflowRunBlock | null = null;
  let selectedAction: ActionsApiResponse | null = null;
  let body: React.ReactNode;

  if (activeItem === null || activeItem === "stream") {
    // "stream" is a live/default selection mode, not a concrete item ID.
    // Resolve it inside the detail panel so polling can move the panel from
    // the currently running block to the final leaf without rewriting the URL.
    const target =
      findRunningBlock(timeline) ?? findLastExecutedBlock(timeline);
    if (target) {
      resolvedBlock = target;
      body = renderBodyForBlock(target, activeItem, onThoughtSelect, timeline);
    } else {
      body = <EmptyState />;
    }
  } else if (isAction(activeItem)) {
    const parentBlock = findBlockSurroundingAction(
      timeline,
      activeItem.action_id,
    );
    if (parentBlock) {
      resolvedBlock = parentBlock;
      selectedAction = activeItem;
      body = renderBodyForBlock(
        parentBlock,
        activeItem,
        onThoughtSelect,
        timeline,
      );
    } else {
      body = <EmptyState />;
    }
  } else if (isObserverThought(activeItem)) {
    resolvedBlock =
      findBlockSurroundingThought(timeline, activeItem.thought_id) ?? null;
    body = <BlockDetailThought thought={activeItem} />;
  } else if (isWorkflowRunBlock(activeItem)) {
    resolvedBlock = activeItem;
    body = renderBodyForBlock(
      activeItem,
      activeItem,
      onThoughtSelect,
      timeline,
    );
  } else {
    body = <EmptyState />;
  }

  // The header slot is always present in the DOM; when no block is resolved
  // the slot is just an empty zero-height div.
  return (
    <>
      <div>
        {resolvedBlock && (
          <>
            <BlockDetailHeader
              block={resolvedBlock}
              iterationOverride={effectiveIteration}
              runFinalized={
                Boolean(workflowRun) &&
                statusIsFinalized({ status: workflowRun!.status })
              }
            />
          </>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        <div>
          {resolvedBlock && (
            <BlockInspector
              block={resolvedBlock}
              action={selectedAction}
              onViewScreenshot={
                onViewScreenshot
                  ? () => onViewScreenshot(resolvedBlock.workflow_run_block_id)
                  : undefined
              }
              statedFailureHeadline={
                resolvedBlock.workflow_run_block_id === statedFailureBlockId
                  ? statedFailureHeadline
                  : null
              }
            />
          )}
          {resolvedBlock && (
            <BlockHealPanel
              workflowRunId={workflowRunId ?? resolvedBlock.workflow_run_id}
              workflowRunBlockId={resolvedBlock.workflow_run_block_id}
            />
          )}
          {resolvedBlock && showDownloadedFiles && (
            <BlockDownloadedFiles
              block={resolvedBlock}
              workflowRunId={workflowRunId}
            />
          )}
          {body}
        </div>
      </div>
    </>
  );
}

export { WorkflowRunBlockDetail };
