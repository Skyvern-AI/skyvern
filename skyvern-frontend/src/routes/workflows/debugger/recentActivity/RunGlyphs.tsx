import {
  CheckCircledIcon,
  CrossCircledIcon,
  DotFilledIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";

import { cn } from "@/util/utils";

import { WorkflowBlockIcon } from "../../editor/nodes/WorkflowBlockIcon";
import type { WorkflowBlockType } from "../../types/workflowTypes";
import { getRunStatusKind } from "./runActivity";

export function RunStatusGlyph({
  status,
  isWorkflowRunning,
  className,
}: {
  status: string;
  isWorkflowRunning: boolean;
  className?: string;
}) {
  const kind = getRunStatusKind(status, isWorkflowRunning);
  if (kind === "success") {
    return (
      <CheckCircledIcon className={cn("shrink-0 text-success", className)} />
    );
  }
  if (kind === "failure") {
    return (
      <CrossCircledIcon
        className={cn("shrink-0 text-destructive", className)}
      />
    );
  }
  if (kind === "running") {
    return (
      <ReloadIcon
        className={cn("shrink-0 animate-spin text-sky-400", className)}
      />
    );
  }
  return (
    <span
      className={cn("flex shrink-0 items-center justify-center", className)}
    >
      <span className="size-2 rounded-full border border-slate-500" />
    </span>
  );
}

export function RunBlockGlyph({
  blockType,
  className,
}: {
  blockType: WorkflowBlockType | undefined;
  className?: string;
}) {
  if (!blockType) {
    return (
      <DotFilledIcon className={cn("shrink-0 text-slate-500", className)} />
    );
  }
  return (
    <WorkflowBlockIcon
      workflowBlockType={blockType}
      className={cn("shrink-0", className)}
    />
  );
}
