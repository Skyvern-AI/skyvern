import { workflowBlockTitle } from "../editor/nodes/types";
import { WorkflowBlockIcon } from "../editor/nodes/WorkflowBlockIcon";
import { WorkflowBlock } from "../types/workflowTypes";

type Props = {
  block: WorkflowBlock;
};

function getTypeLabel(block: WorkflowBlock): string {
  switch (block.block_type) {
    case "conditional":
      return "Condition";
    case "for_loop":
    case "while_loop":
      return "Loop";
    case "navigation":
    case "task":
    case "task_v2":
      return "Task";
    case "http_request":
      return "HTTP";
    default:
      return workflowBlockTitle[block.block_type];
  }
}

function WorkflowRunTimelineUnexecutedBlockItem({ block }: Props) {
  const typeLabel = getTypeLabel(block);
  const blockTypeTitle = workflowBlockTitle[block.block_type];
  return (
    <div className="min-w-0 opacity-60">
      <div className="flex min-h-[28px] items-stretch text-xs">
        <div
          className="flex min-w-0 flex-1 items-center gap-1.5 rounded-r py-1 pr-1.5"
          style={{ marginLeft: "-7px", paddingLeft: "13px" }}
        >
          <div className="size-4 shrink-0" />
          <div className="flex min-w-0 flex-1 items-center gap-1.5">
            <div className="size-2 shrink-0 rounded-full border border-dashed border-slate-500" />
            <span title={blockTypeTitle} className="shrink-0">
              <WorkflowBlockIcon
                workflowBlockType={block.block_type}
                className="size-3.5 text-slate-400"
              />
            </span>
            <span className="inline-flex min-w-[6rem] max-w-[8rem] shrink-0 justify-center truncate rounded bg-slate-700/70 px-1.5 py-0.5 text-[10px] font-medium text-slate-300">
              {typeLabel}
            </span>
            <span className="min-w-0 max-w-[12rem] truncate text-slate-300">
              {block.label}
            </span>
            <span
              className="ml-auto shrink-0 rounded bg-slate-800 px-1 text-[10px] uppercase tracking-wide text-slate-400"
              title="This block did not execute because the workflow ended before reaching it"
            >
              did not execute
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

export { WorkflowRunTimelineUnexecutedBlockItem };
