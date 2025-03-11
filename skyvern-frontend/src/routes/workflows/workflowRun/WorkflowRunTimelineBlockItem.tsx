import {
  CheckCircledIcon,
  CrossCircledIcon,
  CubeIcon,
  ExternalLinkIcon,
} from "@radix-ui/react-icons";
import { workflowBlockTitle } from "../editor/nodes/types";
import { WorkflowBlockIcon } from "../editor/nodes/WorkflowBlockIcon";
import {
  isAction,
  isWorkflowRunBlock,
  WorkflowRunBlock,
} from "../types/workflowRunTypes";
import { ActionCard } from "./ActionCard";
import {
  ActionItem,
  WorkflowRunOverviewActiveElement,
} from "./WorkflowRunOverview";
import { cn } from "@/util/utils";
import { isTaskVariantBlock } from "../types/workflowTypes";
import { Link } from "react-router-dom";
import { useCallback } from "react";
import { Status } from "@/api/types";

type Props = {
  activeItem: WorkflowRunOverviewActiveElement;
  block: WorkflowRunBlock;
  subBlocks: Array<WorkflowRunBlock>;
  onBlockItemClick: (block: WorkflowRunBlock) => void;
  onActionClick: (action: ActionItem) => void;
};

function WorkflowRunTimelineBlockItem({
  activeItem,
  block,
  subBlocks,
  onBlockItemClick,
  onActionClick,
}: Props) {
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
              <span className="text-xs text-slate-400">{block.label}</span>
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
          </div>
        </div>
        {block.description ? (
          <div className="text-xs text-slate-400">{block.description}</div>
        ) : null}
      </div>

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
      {subBlocks.map((block) => {
        return (
          <WorkflowRunTimelineBlockItem
            key={block.workflow_run_block_id}
            block={block}
            activeItem={activeItem}
            onActionClick={onActionClick}
            onBlockItemClick={onBlockItemClick}
            subBlocks={[]}
          />
        );
      })}
    </div>
  );
}

export { WorkflowRunTimelineBlockItem };
