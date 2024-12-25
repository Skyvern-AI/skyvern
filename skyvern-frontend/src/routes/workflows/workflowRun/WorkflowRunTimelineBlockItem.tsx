import {
  isActionItem,
  isWorkflowRunBlock,
  WorkflowRunBlock,
} from "../types/workflowRunTypes";
import { ActionCard } from "./ActionCard";
import { BlockCard } from "./BlockCard";
import {
  ActionItem,
  WorkflowRunOverviewActiveElement,
} from "./WorkflowRunOverview";

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
  const actions = block.actions ? [...block.actions].reverse() : [];

  return (
    <div className="space-y-4 rounded border border-slate-600 p-4">
      {actions.map((action, index) => {
        return (
          <ActionCard
            key={action.action_id}
            action={action}
            active={
              isActionItem(activeItem) &&
              activeItem.action.action_id === action.action_id
            }
            index={actions.length - index}
            onClick={() => {
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
