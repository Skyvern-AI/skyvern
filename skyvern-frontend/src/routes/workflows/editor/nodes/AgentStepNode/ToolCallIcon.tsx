import {
  CheckCircledIcon,
  CursorTextIcon,
  ExternalLinkIcon,
  FileTextIcon,
  GearIcon,
} from "@radix-ui/react-icons";
import type { ComponentType } from "react";

import { ClickIcon } from "@/components/icons/ClickIcon";
import { ExtractIcon } from "@/components/icons/ExtractIcon";

import type { WorkflowBlockType } from "../../../types/workflowTypes";
import { WorkflowBlockIcon } from "../WorkflowBlockIcon";

const TOOL_ICONS: Record<string, ComponentType<{ className?: string }>> = {
  page_goto: ExternalLinkIcon,
  page_click: ClickIcon,
  page_fill: CursorTextIcon,
  page_extract: ExtractIcon,
  read_run_data: FileTextIcon,
  finish: CheckCircledIcon,
};

function ToolCallIcon({
  tool,
  blockType,
  className,
}: {
  tool: string;
  blockType?: WorkflowBlockType | null;
  className?: string;
}) {
  if (blockType) {
    return (
      <WorkflowBlockIcon workflowBlockType={blockType} className={className} />
    );
  }
  const Icon = TOOL_ICONS[tool] ?? GearIcon;
  return <Icon className={className} />;
}

export { ToolCallIcon };
