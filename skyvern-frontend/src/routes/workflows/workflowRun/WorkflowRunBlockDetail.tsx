import type { ActionsApiResponse } from "@/api/types";
import { useState } from "react";
import { ActionCardCompact } from "@/routes/tasks/detail/ActionCardCompact";
import {
  isAction,
  isObserverThought,
  isWorkflowRunBlock,
  type ObserverThought,
  type WorkflowRunBlock,
  type WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
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
  onActionSelect?: (payload: {
    block: WorkflowRunBlock;
    action: ActionsApiResponse;
  }) => void;
  onThoughtSelect?: (thought: ObserverThought) => void;
};

function isLoopBlock(block: WorkflowRunBlock): boolean {
  return block.block_type === "for_loop" || block.block_type === "while_loop";
}

function SelectedActionHeader({
  action,
  block,
  index,
  onActionSelect,
}: {
  action: ActionsApiResponse;
  block: WorkflowRunBlock;
  index: number;
  onActionSelect?: Props["onActionSelect"];
}) {
  const [expanded, setExpanded] = useState(true);

  return (
    <div className="border-b border-slate-700 bg-slate-elevation1 px-3 py-2 duration-200 animate-in fade-in slide-in-from-top-1">
      <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-slate-500">
        Selected action
      </div>
      <ActionCardCompact
        action={action}
        active
        index={index}
        expanded={expanded}
        onToggleExpanded={() => setExpanded((prev) => !prev)}
        onSelect={() => onActionSelect?.({ block, action })}
        cardClassName="bg-slate-800/70"
      />
    </div>
  );
}

function getActionDisplayIndex(
  block: WorkflowRunBlock,
  action: ActionsApiResponse,
): number {
  const actionsTopDown = [...(block.actions ?? [])].reverse();
  const index = actionsTopDown.findIndex(
    (item) => item.action_id === action.action_id,
  );
  return index === -1 ? 1 : index + 1;
}

function renderBodyForBlock(
  block: WorkflowRunBlock,
  activeItem: WorkflowRunOverviewActiveElement,
  onActionSelect: Props["onActionSelect"],
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
          onActionSelect={onActionSelect}
          onThoughtSelect={onThoughtSelect}
          thoughts={thoughts}
        />
      );
    case "conditional":
      return (
        <BlockDetailConditional
          block={block}
          activeItem={activeItem}
          onActionSelect={onActionSelect}
        />
      );
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
  onActionSelect,
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
  let selectedActionIndex = 1;
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
        onActionSelect,
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
      selectedActionIndex = getActionDisplayIndex(parentBlock, activeItem);
      body = renderBodyForBlock(
        parentBlock,
        activeItem,
        onActionSelect,
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
      onActionSelect,
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
          {resolvedBlock && selectedAction && (
            <SelectedActionHeader
              action={selectedAction}
              block={resolvedBlock}
              index={selectedActionIndex}
              onActionSelect={onActionSelect}
            />
          )}
          {resolvedBlock && <BlockInspector block={resolvedBlock} />}
          {body}
        </div>
      </div>
    </>
  );
}

export { WorkflowRunBlockDetail };
