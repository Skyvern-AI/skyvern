import { memo } from "react";
import { CodeBlockNode as CodeBlockNodeComponent } from "./CodeBlockNode/CodeBlockNode";
import { CodeBlockNode } from "./CodeBlockNode/types";
import { ConditionalNode as ConditionalNodeComponent } from "./ConditionalNode/ConditionalNode";
import type { ConditionalNode } from "./ConditionalNode/types";
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
import type { FileUploadNode } from "./FileUploadNode/types";
import { FileUploadNode as FileUploadNodeComponent } from "./FileUploadNode/FileUploadNode";
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
import { LoginNode } from "./LoginNode/types";
import { LoginNode as LoginNodeComponent } from "./LoginNode/LoginNode";
import { WaitNode } from "./WaitNode/types";
import { WaitNode as WaitNodeComponent } from "./WaitNode/WaitNode";
import { FileDownloadNode } from "./FileDownloadNode/types";
import { FileDownloadNode as FileDownloadNodeComponent } from "./FileDownloadNode/FileDownloadNode";
import { PDFParserNode } from "./PDFParserNode/types";
import { PDFParserNode as PDFParserNodeComponent } from "./PDFParserNode/PDFParserNode";
import { Taskv2Node } from "./Taskv2Node/types";
import { Taskv2Node as Taskv2NodeComponent } from "./Taskv2Node/Taskv2Node";
import { URLNode } from "./URLNode/types";
import { URLNode as URLNodeComponent } from "./URLNode/URLNode";
import { HttpRequestNode } from "./HttpRequestNode/types";
import { HttpRequestNode as HttpRequestNodeComponent } from "./HttpRequestNode/HttpRequestNode";
import { HumanInteractionNode } from "./HumanInteractionNode/types";
import { HumanInteractionNode as HumanInteractionNodeComponent } from "./HumanInteractionNode/HumanInteractionNode";

export type UtilityNode = StartNode | NodeAdderNode;

export type WorkflowBlockNode =
  | LoopNode
  | ConditionalNode
  | TaskNode
  | TextPromptNode
  | SendEmailNode
  | CodeBlockNode
  | FileParserNode
  | UploadNode
  | FileUploadNode
  | DownloadNode
  | ValidationNode
  | HumanInteractionNode
  | ActionNode
  | NavigationNode
  | ExtractionNode
  | LoginNode
  | WaitNode
  | FileDownloadNode
  | PDFParserNode
  | Taskv2Node
  | URLNode
  | HttpRequestNode;

export function isUtilityNode(node: AppNode): node is UtilityNode {
  return node.type === "nodeAdder" || node.type === "start";
}

export function isWorkflowBlockNode(node: AppNode): node is WorkflowBlockNode {
  return node.type !== "nodeAdder" && node.type !== "start";
}

export type AppNode = UtilityNode | WorkflowBlockNode;

export const nodeTypes = {
  loop: memo(LoopNodeComponent),
  conditional: memo(ConditionalNodeComponent),
  task: memo(TaskNodeComponent),
  textPrompt: memo(TextPromptNodeComponent),
  sendEmail: memo(SendEmailNodeComponent),
  codeBlock: memo(CodeBlockNodeComponent),
  fileParser: memo(FileParserNodeComponent),
  upload: memo(UploadNodeComponent),
  fileUpload: memo(FileUploadNodeComponent),
  download: memo(DownloadNodeComponent),
  nodeAdder: memo(NodeAdderNodeComponent),
  start: memo(StartNodeComponent),
  validation: memo(ValidationNodeComponent),
  action: memo(ActionNodeComponent),
  navigation: memo(NavigationNodeComponent),
  human_interaction: memo(HumanInteractionNodeComponent),
  extraction: memo(ExtractionNodeComponent),
  login: memo(LoginNodeComponent),
  wait: memo(WaitNodeComponent),
  fileDownload: memo(FileDownloadNodeComponent),
  pdfParser: memo(PDFParserNodeComponent),
  taskv2: memo(Taskv2NodeComponent),
  url: memo(URLNodeComponent),
  http_request: memo(HttpRequestNodeComponent),
} as const;
