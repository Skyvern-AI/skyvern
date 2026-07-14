import type { ActionsApiResponse } from "@/api/types";
import { useMemo } from "react";
import { FileIcon } from "@radix-ui/react-icons";
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
import { BlockDetailLoop } from "./blockDetail/BlockDetailLoop";
import { BlockDetailTask } from "./blockDetail/BlockDetailTask";
import { BlockDetailThought } from "./blockDetail/BlockDetailThought";
import { BlockDetailWorkflowTrigger } from "./blockDetail/BlockDetailWorkflowTrigger";
import { BlockInspector } from "./blockDetail/BlockInspector";
import { EmptyState } from "./blockDetail/EmptyState";
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
  workflowRunId?: string;
  onThoughtSelect?: (thought: ObserverThought) => void;
};

function isLoopBlock(block: WorkflowRunBlock): boolean {
  return block.block_type === "for_loop" || block.block_type === "while_loop";
}

function BlockDownloadedFiles({
  block,
  workflowRunId,
}: {
  block: WorkflowRunBlock;
  workflowRunId?: string;
}) {
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery(
    workflowRunId ? { workflowRunId } : undefined,
  );
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
            <a
              href={file.url}
              className="truncate underline underline-offset-4"
            >
              {file.filename}
            </a>
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
  activeIteration: number | null,
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
    case "for_loop":
    case "while_loop":
      return <BlockDetailLoop block={block} iterationIndex={activeIteration} />;
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
}: Props) {
  // activeIteration is a URL hint scoped to a specific selection. In
  // fallback mode (null or "stream") the resolved block may not be the
  // loop the iteration was set for — ignore it to avoid stale labels.
  const effectiveIteration =
    activeItem === null || activeItem === "stream" ? null : activeIteration;

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
      body = renderBodyForBlock(
        target,
        activeItem,
        onThoughtSelect,
        effectiveIteration,
        timeline,
      );
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
        effectiveIteration,
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
      effectiveIteration,
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
              iterationOverride={
                isLoopBlock(resolvedBlock) ? effectiveIteration : null
              }
            />
          </>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        <div>
          {resolvedBlock && (
            <BlockInspector block={resolvedBlock} action={selectedAction} />
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
