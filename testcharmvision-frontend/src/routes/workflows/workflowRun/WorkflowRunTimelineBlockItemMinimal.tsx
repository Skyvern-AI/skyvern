import { Tip } from "@/components/Tip";
import { workflowBlockTitle } from "../editor/nodes/types";
import { WorkflowBlockIcon } from "../editor/nodes/WorkflowBlockIcon";
import {
  isBlockItem,
  isThoughtItem,
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import { ActionCardMinimal } from "./ActionCardMinimal";
import { Status } from "@/api/types";
import { ThoughtCardMinimal } from "./ThoughtCardMinimal";
import { ItemStatusIndicator } from "./ItemStatusIndicator";
import { cn } from "@/util/utils";

type Props = {
  block: WorkflowRunBlock;
  subItems: Array<WorkflowRunTimelineItem>;
};

function WorkflowRunTimelineBlockItemMinimal({ block, subItems }: Props) {
  const actions = block.actions ?? [];
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
      className={cn("flex flex-col items-center justify-center gap-2", {
        "rounded-lg bg-slate-elevation4 pl-2 pr-3 pt-4": actions.length > 0,
      })}
    >
      <Tip
        content={workflowBlockTitle[block.block_type] ?? null}
        asChild={false}
      >
        <ItemStatusIndicator
          failure={showFailureIndicator}
          success={showSuccessIndicator}
        >
          <WorkflowBlockIcon workflowBlockType={block.block_type} />
        </ItemStatusIndicator>
      </Tip>

      {actions.length > 0 && (
        <div className="flex flex-col items-center justify-center gap-4 rounded-md p-2">
          {actions.map((action) => {
            return <ActionCardMinimal key={action.action_id} action={action} />;
          })}
        </div>
      )}
      {subItems.map((item) => {
        if (isBlockItem(item)) {
          return (
            <WorkflowRunTimelineBlockItemMinimal
              key={item.block.workflow_run_block_id}
              subItems={item.children}
              block={item.block}
            />
          );
        }
        if (isThoughtItem(item)) {
          return (
            <ThoughtCardMinimal
              key={item.thought.thought_id}
              thought={item.thought}
            />
          );
        }
      })}
    </div>
  );
}

export { WorkflowRunTimelineBlockItemMinimal };
