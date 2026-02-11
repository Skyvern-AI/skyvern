import { CubeIcon } from "@radix-ui/react-icons";
import { WorkflowRunBlock } from "../types/workflowRunTypes";
import { cn } from "@/util/utils";
import { WorkflowBlockIcon } from "../editor/nodes/WorkflowBlockIcon";
import { workflowBlockTitle } from "../editor/nodes/types";

type Props = {
  active: boolean;
  block: WorkflowRunBlock;
  onClick: () => void;
};

function BlockCard({ block, onClick, active }: Props) {
  return (
    <div
      className={cn(
        "cursor-pointer space-y-3 rounded-md border bg-slate-elevation3 p-4 hover:border-slate-50",
        {
          "border-slate-50": active,
        },
      )}
      onClick={onClick}
    >
      <div className="flex justify-between">
        <div className="flex gap-3">
          <WorkflowBlockIcon
            workflowBlockType={block.block_type}
            className="size-6"
          />
          <span>{workflowBlockTitle[block.block_type]}</span>
        </div>
        <div className="flex items-center gap-1 rounded bg-slate-elevation5 px-2 py-1">
          <CubeIcon className="size-4" />
          <span className="text-xs">Block</span>
        </div>
      </div>
    </div>
  );
}

export { BlockCard };
