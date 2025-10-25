import {
  CheckCircledIcon,
  CrossCircledIcon,
  CubeIcon,
  ExternalLinkIcon,
} from "@radix-ui/react-icons";
import { useCallback } from "react";
import { useParams, Link } from "react-router-dom";

import { Status } from "@/api/types";
import { formatDuration, toDuration } from "@/routes/workflows/utils";
import { cn } from "@/util/utils";
import { workflowBlockTitle } from "../editor/nodes/types";
import { WorkflowBlockIcon } from "../editor/nodes/WorkflowBlockIcon";
import {
  isAction,
  isBlockItem,
  isObserverThought,
  isThoughtItem,
  isWorkflowRunBlock,
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import { ActionCard } from "./ActionCard";
import {
  ActionItem,
  WorkflowRunOverviewActiveElement,
} from "./WorkflowRunOverview";
import { ThoughtCard } from "./ThoughtCard";
import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
import { ObserverThought } from "../types/workflowRunTypes";
import {
  HumanInteractionBlock,
  isTaskVariantBlock,
  type WorkflowApiResponse,
} from "../types/workflowTypes";
import { WorkflowRunHumanInteraction } from "./WorkflowRunHumanInteraction";

type Props = {
  activeItem: WorkflowRunOverviewActiveElement;
  block: WorkflowRunBlock;
  subItems: Array<WorkflowRunTimelineItem>;
  onBlockItemClick: (block: WorkflowRunBlock) => void;
  onActionClick: (action: ActionItem) => void;
  onThoughtCardClick: (thought: ObserverThought) => void;
};

const getHumanInteractionBlock = (
  block: WorkflowRunBlock,
  workflow?: WorkflowApiResponse,
): HumanInteractionBlock | null => {
  if (block.block_type !== "human_interaction") {
    return null;
  }

  if (!workflow) {
    return null;
  }

  const blocks = workflow.workflow_definition.blocks;
  const candidate = blocks.find((b) => b.label === block.label);

  if (!candidate || candidate.block_type !== "human_interaction") {
    return null;
  }

  return candidate as HumanInteractionBlock;
};

function WorkflowRunTimelineBlockItem({
  activeItem,
  block,
  subItems,
  onBlockItemClick,
  onActionClick,
  onThoughtCardClick,
}: Props) {
  const { workflowPermanentId } = useParams();
  const { data: workflow } = useWorkflowQuery({
    workflowPermanentId,
  });

  const humanInteractionBlock = getHumanInteractionBlock(block, workflow);
  const actions = block.actions ?? [];

  const hasActiveAction =
    isAction(activeItem) &&
    Boolean(
      block.actions?.find(
        (action) => action.action_id === activeItem.action_id,
      ),
    );
  const isActiveBlock =
    isWorkflowRunBlock(activeItem) &&
    activeItem.workflow_run_block_id === block.workflow_run_block_id;

  const showDiagnosticLink =
    isTaskVariantBlock(block) && (hasActiveAction || isActiveBlock);

  const refCallback = useCallback((element: HTMLDivElement | null) => {
    if (
      element &&
      isWorkflowRunBlock(activeItem) &&
      activeItem.workflow_run_block_id === block.workflow_run_block_id
    ) {
      element.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }
    // this should only run once at mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const showStatusIndicator = block.status !== null;
  const showSuccessIndicator =
    showStatusIndicator && block.status === Status.Completed;
  const showFailureIndicator =
    showStatusIndicator &&
    (block.status === Status.Failed ||
      block.status === Status.Terminated ||
      block.status === Status.TimedOut ||
      block.status === Status.Canceled);

  const duration =
    block.duration !== null ? formatDuration(toDuration(block.duration)) : null;

  // NOTE(jdo): want to put this back; await for now
  const showDuration = false as const;

  return (
    <div
      className={cn(
        "cursor-pointer space-y-4 rounded border border-slate-600 p-4",
        {
          "border-slate-50":
            isWorkflowRunBlock(activeItem) &&
            activeItem.workflow_run_block_id === block.workflow_run_block_id,
        },
      )}
      onClick={(event) => {
        event.stopPropagation();
        onBlockItemClick(block);
      }}
      ref={refCallback}
    >
      <div className="space-y-2">
        <div className="flex justify-between">
          <div className="flex gap-3">
            <div className="rounded bg-slate-elevation5 p-2">
              <WorkflowBlockIcon
                workflowBlockType={block.block_type}
                className="size-6"
              />
            </div>

            <div className="flex flex-col gap-1">
              <span className="text-sm">
                {workflowBlockTitle[block.block_type]}
              </span>
              <span className="flex gap-2 text-xs text-slate-400">
                {block.label}
              </span>
            </div>
          </div>
          <div className="flex gap-2">
            {showFailureIndicator && (
              <div className="self-start rounded bg-slate-elevation5 px-2 py-1">
                <CrossCircledIcon className="size-4 text-destructive" />
              </div>
            )}
            {showSuccessIndicator && (
              <div className="self-start rounded bg-slate-elevation5 px-2 py-1">
                <CheckCircledIcon className="size-4 text-success" />
              </div>
            )}
            <div className="flex flex-col items-end gap-[1px]">
              <div className="flex gap-1 self-start rounded bg-slate-elevation5 px-2 py-1">
                {showDiagnosticLink ? (
                  <Link to={`/tasks/${block.task_id}/diagnostics`}>
                    <div className="flex gap-1">
                      <ExternalLinkIcon className="size-4" />
                      <span className="text-xs">Diagnostics</span>
                    </div>
                  </Link>
                ) : (
                  <>
                    <CubeIcon className="size-4" />
                    <span className="text-xs">Block</span>
                  </>
                )}
              </div>
              {duration && showDuration && (
                <div className="pr-[5px] text-xs text-[#00ecff]">
                  {duration}
                </div>
              )}
            </div>
          </div>
        </div>
        {block.description ? (
          <div className="text-xs text-slate-400">{block.description}</div>
        ) : null}
      </div>

      {humanInteractionBlock && (
        <WorkflowRunHumanInteraction
          humanInteractionBlock={humanInteractionBlock}
        />
      )}

      {actions.map((action, index) => {
        return (
          <ActionCard
            key={action.action_id}
            action={action}
            active={
              isAction(activeItem) && activeItem.action_id === action.action_id
            }
            index={actions.length - index}
            onClick={(event) => {
              event.stopPropagation();
              const actionItem: ActionItem = {
                block,
                action,
              };
              onActionClick(actionItem);
            }}
          />
        );
      })}
      {subItems.map((item) => {
        if (isBlockItem(item)) {
          return (
            <WorkflowRunTimelineBlockItem
              key={item.block.workflow_run_block_id}
              subItems={item.children}
              activeItem={activeItem}
              block={item.block}
              onActionClick={onActionClick}
              onBlockItemClick={onBlockItemClick}
              onThoughtCardClick={onThoughtCardClick}
            />
          );
        }
        if (isThoughtItem(item)) {
          return (
            <ThoughtCard
              key={item.thought.thought_id}
              active={
                isObserverThought(activeItem) &&
                activeItem.thought_id === item.thought.thought_id
              }
              onClick={onThoughtCardClick}
              thought={item.thought}
            />
          );
        }
      })}
    </div>
  );
}

export { WorkflowRunTimelineBlockItem };
