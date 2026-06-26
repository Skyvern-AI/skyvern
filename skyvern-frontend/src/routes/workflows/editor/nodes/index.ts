import type { NodeProps } from "@xyflow/react";
import { memo, type ComponentType } from "react";
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
import { PrintPageNode } from "./PrintPageNode/types";
import { PrintPageNode as PrintPageNodeComponent } from "./PrintPageNode/PrintPageNode";
import { WorkflowTriggerNode } from "./WorkflowTriggerNode/types";
import { WorkflowTriggerNode as WorkflowTriggerNodeComponent } from "./WorkflowTriggerNode/WorkflowTriggerNode";
import { GoogleSheetsReadNode } from "./GoogleSheetsReadNode/types";
import { GoogleSheetsReadNode as GoogleSheetsReadNodeComponent } from "./GoogleSheetsReadNode/GoogleSheetsReadNode";
import { GoogleSheetsWriteNode } from "./GoogleSheetsWriteNode/types";
import { GoogleSheetsWriteNode as GoogleSheetsWriteNodeComponent } from "./GoogleSheetsWriteNode/GoogleSheetsWriteNode";
import { PdfFillNode } from "./PdfFillNode/types";
import { PdfFillNode as PdfFillNodeComponent } from "./PdfFillNode/PdfFillNode";
import { withSortableBlock } from "../sortable/withSortableBlock";
import { withCollapsible } from "../collapse/withCollapsible";
import { withSelectableBlock } from "../selection/withSelectableBlock";

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
  | HttpRequestNode
  | PrintPageNode
  | WorkflowTriggerNode
  | GoogleSheetsReadNode
  | GoogleSheetsWriteNode
  | PdfFillNode;

export function isUtilityNode(node: AppNode): node is UtilityNode {
  return node.type === "nodeAdder" || node.type === "start";
}

export function isWorkflowBlockNode(node: AppNode): node is WorkflowBlockNode {
  return node.type !== "nodeAdder" && node.type !== "start";
}

export type AppNode = UtilityNode | WorkflowBlockNode;

// Composition order is load-bearing:
//   memo (outermost)    - stable identity per node type for RF reconciliation
//   withSortableBlock   - registers `useSortable({ id })`; the inner tree
//                         must mount in both open and collapsed states so
//                         drag pickup works on either
//   withSelectableBlock - reads `useSortable.isDragging` to suppress
//                         selection on drag pickup
//   withCollapsible     - leaf wrapper for body chrome
function wrapBlock<P extends NodeProps>(Component: ComponentType<P>) {
  return memo(
    withSortableBlock(withSelectableBlock(withCollapsible(Component))),
  );
}

// `loop` and `conditional` are containers — RF renders their bodies as
// child nodes, so collapsing the parent to a header-only card would leave
// children visually overflowing the card and break edge layout.
function wrapContainerBlock<P extends NodeProps>(Component: ComponentType<P>) {
  return memo(withSortableBlock(withSelectableBlock(Component)));
}

export const nodeTypes = {
  loop: wrapContainerBlock(LoopNodeComponent),
  conditional: wrapContainerBlock(ConditionalNodeComponent),
  task: wrapBlock(TaskNodeComponent),
  textPrompt: wrapBlock(TextPromptNodeComponent),
  sendEmail: wrapBlock(SendEmailNodeComponent),
  codeBlock: wrapBlock(CodeBlockNodeComponent),
  fileParser: wrapBlock(FileParserNodeComponent),
  upload: wrapBlock(UploadNodeComponent),
  fileUpload: wrapBlock(FileUploadNodeComponent),
  download: wrapBlock(DownloadNodeComponent),
  nodeAdder: memo(NodeAdderNodeComponent),
  start: memo(StartNodeComponent),
  validation: wrapBlock(ValidationNodeComponent),
  action: wrapBlock(ActionNodeComponent),
  navigation: wrapBlock(NavigationNodeComponent),
  human_interaction: wrapBlock(HumanInteractionNodeComponent),
  extraction: wrapBlock(ExtractionNodeComponent),
  login: wrapBlock(LoginNodeComponent),
  wait: wrapBlock(WaitNodeComponent),
  fileDownload: wrapBlock(FileDownloadNodeComponent),
  pdfParser: wrapBlock(PDFParserNodeComponent),
  taskv2: wrapBlock(Taskv2NodeComponent),
  url: wrapBlock(URLNodeComponent),
  http_request: wrapBlock(HttpRequestNodeComponent),
  printPage: wrapBlock(PrintPageNodeComponent),
  workflowTrigger: wrapBlock(WorkflowTriggerNodeComponent),
  googleSheetsRead: wrapBlock(GoogleSheetsReadNodeComponent),
  googleSheetsWrite: wrapBlock(GoogleSheetsWriteNodeComponent),
  pdfFill: wrapBlock(PdfFillNodeComponent),
} as const;
