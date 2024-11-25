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
import type { NodeAdderNode } from "./NodeAdderNode/types";
import { NodeAdderNode as NodeAdderNodeComponent } from "./NodeAdderNode/NodeAdderNode";
import { StartNode as StartNodeComponent } from "./StartNode/StartNode";
import type { StartNode } from "./StartNode/types";
import type { ValidationNode } from "./ValidationNode/types";
import { ValidationNode as ValidationNodeComponent } from "./ValidationNode/ValidationNode";
import type { ActionNode } from "./ActionNode/types";
import { ActionNode as ActionNodeComponent } from "./ActionNode/ActionNode";
import { NavigationNode } from "./NavigationNode/types";
import { NavigationNode as NavigationNodeComponent } from "./NavigationNode/NavigationNode";
import { ExtractionNode } from "./ExtractionNode/types";
import { ExtractionNode as ExtractionNodeComponent } from "./ExtractionNode/ExtractionNode";

export type UtilityNode = StartNode | NodeAdderNode;

export type WorkflowBlockNode =
  | LoopNode
  | TaskNode
  | TextPromptNode
  | SendEmailNode
  | CodeBlockNode
  | FileParserNode
  | UploadNode
  | DownloadNode
  | ValidationNode
  | ActionNode
  | NavigationNode
  | ExtractionNode;

export function isUtilityNode(node: AppNode): node is UtilityNode {
  return node.type === "nodeAdder" || node.type === "start";
}

export function isWorkflowBlockNode(node: AppNode): node is WorkflowBlockNode {
  return node.type !== "nodeAdder" && node.type !== "start";
}

export type AppNode = UtilityNode | WorkflowBlockNode;

export const nodeTypes = {
  loop: memo(LoopNodeComponent),
  task: memo(TaskNodeComponent),
  textPrompt: memo(TextPromptNodeComponent),
  sendEmail: memo(SendEmailNodeComponent),
  codeBlock: memo(CodeBlockNodeComponent),
  fileParser: memo(FileParserNodeComponent),
  upload: memo(UploadNodeComponent),
  download: memo(DownloadNodeComponent),
  nodeAdder: memo(NodeAdderNodeComponent),
  start: memo(StartNodeComponent),
  validation: memo(ValidationNodeComponent),
  action: memo(ActionNodeComponent),
  navigation: memo(NavigationNodeComponent),
  extraction: memo(ExtractionNodeComponent),
} as const;
