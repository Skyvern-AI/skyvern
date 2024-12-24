import { ActionsApiResponse } from "@/api/types";
import {
  isAction,
  isWorkflowRunBlock,
  WorkflowRunBlock,
} from "../types/workflowRunTypes";
import { ActionCard } from "./ActionCard";
import { BlockCard } from "./BlockCard";
import { WorkflowRunOverviewActiveElement } from "./WorkflowRunOverview";

type Props = {
  activeItem: WorkflowRunOverviewActiveElement;
  block: WorkflowRunBlock;
  subBlocks: Array<WorkflowRunBlock>;
  onBlockItemClick: (block: WorkflowRunBlock) => void;
  onActionClick: (action: ActionsApiResponse) => void;
};

function WorkflowRunTimelineBlockItem({
  activeItem,
  block,
  subBlocks,
  onBlockItemClick,
  onActionClick,
}: Props) {
  const actions = block.actions ? [...block.actions].reverse() : [];

  return (
    <div className="space-y-4 rounded border border-slate-600 p-4">
      {actions.map((action, index) => {
        return (
          <ActionCard
            key={action.action_id}
            action={action}
            active={
              isAction(activeItem) && activeItem.action_id === action.action_id
            }
            index={actions.length - index}
            onClick={() => {
              onActionClick(action);
            }}
          />
        );
      })}
      {subBlocks.map((block) => {
        return (
          <WorkflowRunTimelineBlockItem
            block={block}
            activeItem={activeItem}
            onActionClick={onActionClick}
            onBlockItemClick={onBlockItemClick}
            subBlocks={[]}
          />
        );
      })}
      <BlockCard
        active={
          isWorkflowRunBlock(activeItem) &&
          activeItem.workflow_run_block_id === block.workflow_run_block_id
        }
        block={block}
        onClick={() => {
          onBlockItemClick(block);
        }}
      />
    </div>
  );
}

export { WorkflowRunTimelineBlockItem };
