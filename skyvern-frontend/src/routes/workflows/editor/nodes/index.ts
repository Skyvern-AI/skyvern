import { memo } from "react";
import { CodeBlockNode as CodeBlockNodeComponent } from "./CodeBlockNode/CodeBlockNode";
import { CodeBlockNode } from "./CodeBlockNode/types";
import { LoopNode as LoopNodeComponent } from "./LoopNode/LoopNode";
import type { LoopNode } from "./LoopNode/types";
import { SendEmailNode as SendEmailNodeComponent } from "./SendEmailNode/SendEmailNode";
import type { SendEmailNode } from "./SendEmailNode/types";
import { TaskNode as TaskNodeComponent } from "./TaskNode/TaskNode";
import type { TaskNode } from "./TaskNode/types";
import { TextPromptNode as TextPromptNodeComponent } from "./TextPromptNode/TextPromptNode";
import type { TextPromptNode } from "./TextPromptNode/types";
import type { FileParserNode } from "./FileParserNode/types";
import { FileParserNode as FileParserNodeComponent } from "./FileParserNode/FileParserNode";
import type { UploadNode } from "./UploadNode/types";
import { UploadNode as UploadNodeComponent } from "./UploadNode/UploadNode";
import type { DownloadNode } from "./DownloadNode/types";
import { DownloadNode as DownloadNodeComponent } from "./DownloadNode/DownloadNode";

export type AppNode =
  | LoopNode
  | TaskNode
  | TextPromptNode
  | SendEmailNode
  | CodeBlockNode
  | FileParserNode
  | UploadNode
  | DownloadNode;

export const nodeTypes = {
  loop: memo(LoopNodeComponent),
  task: memo(TaskNodeComponent),
  textPrompt: memo(TextPromptNodeComponent),
  sendEmail: memo(SendEmailNodeComponent),
  codeBlock: memo(CodeBlockNodeComponent),
  fileParser: memo(FileParserNodeComponent),
  upload: memo(UploadNodeComponent),
  download: memo(DownloadNodeComponent),
};
