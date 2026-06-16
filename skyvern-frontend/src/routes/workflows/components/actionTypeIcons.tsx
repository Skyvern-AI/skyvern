import {
  CheckCircledIcon,
  CrossCircledIcon,
  CursorArrowIcon,
  Cross2Icon,
  DotFilledIcon,
  DoubleArrowDownIcon,
  DropdownMenuIcon,
  FileTextIcon,
  HandIcon,
  InputIcon,
  KeyboardIcon,
  MagicWandIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import type { ReactNode } from "react";

import { type ActionType, ActionTypes } from "@/api/types";

import { WorkflowBlockIcon } from "../editor/nodes/WorkflowBlockIcon";

export const actionTypeIcons: Record<ActionType, ReactNode> = {
  [ActionTypes.Click]: (
    <WorkflowBlockIcon workflowBlockType="action" className="size-3.5" />
  ),
  [ActionTypes.Hover]: <HandIcon className="size-3.5" />,
  [ActionTypes.InputText]: <InputIcon className="size-3.5" />,
  [ActionTypes.DownloadFile]: (
    <WorkflowBlockIcon workflowBlockType="file_download" className="size-3.5" />
  ),
  [ActionTypes.UploadFile]: (
    <WorkflowBlockIcon workflowBlockType="file_upload" className="size-3.5" />
  ),
  [ActionTypes.SelectOption]: <DropdownMenuIcon className="size-3.5" />,
  [ActionTypes.complete]: <CheckCircledIcon className="size-3.5" />,
  [ActionTypes.wait]: (
    <WorkflowBlockIcon workflowBlockType="wait" className="size-3.5" />
  ),
  [ActionTypes.terminate]: <CrossCircledIcon className="size-3.5" />,
  [ActionTypes.SolveCaptcha]: <MagicWandIcon className="size-3.5" />,
  [ActionTypes.extract]: (
    <WorkflowBlockIcon workflowBlockType="extraction" className="size-3.5" />
  ),
  [ActionTypes.ReloadPage]: <ReloadIcon className="size-3.5" />,
  [ActionTypes.Scroll]: <DoubleArrowDownIcon className="size-3.5" />,
  [ActionTypes.KeyPress]: <KeyboardIcon className="size-3.5" />,
  [ActionTypes.Move]: <CursorArrowIcon className="size-3.5" />,
  [ActionTypes.NullAction]: <FileTextIcon className="size-3.5" />,
  [ActionTypes.VerificationCode]: <KeyboardIcon className="size-3.5" />,
  [ActionTypes.Drag]: <HandIcon className="size-3.5" />,
  [ActionTypes.LeftMouse]: (
    <WorkflowBlockIcon workflowBlockType="action" className="size-3.5" />
  ),
  [ActionTypes.GotoUrl]: (
    <WorkflowBlockIcon workflowBlockType="goto_url" className="size-3.5" />
  ),
  [ActionTypes.ClosePage]: <Cross2Icon className="size-3.5" />,
};

export function getActionTypeIcon(actionType: string): ReactNode {
  return (
    actionTypeIcons[actionType as ActionType] ?? (
      <DotFilledIcon className="size-3.5" />
    )
  );
}
