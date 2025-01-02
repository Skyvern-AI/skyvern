import { ExternalLinkIcon } from "@radix-ui/react-icons";
import { WorkflowRunBlock } from "../types/workflowRunTypes";
import { cn } from "@/util/utils";
import { WorkflowBlockIcon } from "../editor/nodes/WorkflowBlockIcon";
import { workflowBlockTitle } from "../editor/nodes/types";
import { Button } from "@/components/ui/button";
import { Link } from "react-router-dom";

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
      <div className="flex items-center justify-between">
        <div className="flex gap-3">
          <WorkflowBlockIcon
            workflowBlockType={block.block_type}
            className="size-6"
          />
          <span>{workflowBlockTitle[block.block_type]}</span>
        </div>
        {block.task_id && (
          <Button
            title="Go to task diagnostics"
            asChild
            size="icon"
            className="size-8 bg-slate-800 text-primary hover:bg-slate-700"
          >
            <Link to={`/tasks/${block.task_id}/diagnostics`}>
              <ExternalLinkIcon />
            </Link>
          </Button>
        )}
      </div>
    </div>
  );
}

export { BlockCard };
