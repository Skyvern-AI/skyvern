import Dagre from "@dagrejs/dagre";
import type { Node } from "@xyflow/react";
import { Edge } from "@xyflow/react";
import { nanoid } from "nanoid";
import {
  WorkflowBlockType,
  WorkflowBlockTypes,
  WorkflowParameterTypes,
  WorkflowParameterValueType,
  type AWSSecretParameter,
  type OutputParameter,
  type Parameter,
  type WorkflowApiResponse,
  type WorkflowBlock,
  type WorkflowSettings,
} from "../types/workflowTypes";
import {
  ActionBlockYAML,
  BlockYAML,
  CodeBlockYAML,
  DownloadToS3BlockYAML,
  FileUrlParserBlockYAML,
  ForLoopBlockYAML,
  ParameterYAML,
  SendEmailBlockYAML,
  TaskBlockYAML,
  TextPromptBlockYAML,
  UploadToS3BlockYAML,
  ValidationBlockYAML,
  NavigationBlockYAML,
  WorkflowCreateYAMLRequest,
  ExtractionBlockYAML,
  LoginBlockYAML,
  WaitBlockYAML,
  FileDownloadBlockYAML,
  PDFParserBlockYAML,
  Taskv2BlockYAML,
  URLBlockYAML,
  FileUploadBlockYAML,
} from "../types/workflowYamlTypes";
import {
  EMAIL_BLOCK_SENDER,
  REACT_FLOW_EDGE_Z_INDEX,
  SMTP_HOST_AWS_KEY,
  SMTP_HOST_PARAMETER_KEY,
  SMTP_PASSWORD_AWS_KEY,
  SMTP_PASSWORD_PARAMETER_KEY,
  SMTP_PORT_AWS_KEY,
  SMTP_PORT_PARAMETER_KEY,
  SMTP_USERNAME_AWS_KEY,
  SMTP_USERNAME_PARAMETER_KEY,
} from "./constants";
import { ParametersState } from "./types";
import { AppNode, isWorkflowBlockNode, WorkflowBlockNode } from "./nodes";
import { codeBlockNodeDefaultData } from "./nodes/CodeBlockNode/types";
import { downloadNodeDefaultData } from "./nodes/DownloadNode/types";
import { fileParserNodeDefaultData } from "./nodes/FileParserNode/types";
import {
  isLoopNode,
  LoopNode,
  loopNodeDefaultData,
} from "./nodes/LoopNode/types";
import { NodeAdderNode } from "./nodes/NodeAdderNode/types";
import { sendEmailNodeDefaultData } from "./nodes/SendEmailNode/types";
import {
  isStartNode,
  isWorkflowStartNodeData,
  StartNode,
  StartNodeData,
} from "./nodes/StartNode/types";
import { isTaskNode, taskNodeDefaultData } from "./nodes/TaskNode/types";
import {
  isTextPromptNode,
  textPromptNodeDefaultData,
} from "./nodes/TextPromptNode/types";
import { NodeBaseData } from "./nodes/types";
import { uploadNodeDefaultData } from "./nodes/UploadNode/types";
import {
  isValidationNode,
  validationNodeDefaultData,
} from "./nodes/ValidationNode/types";
import { actionNodeDefaultData, isActionNode } from "./nodes/ActionNode/types";
import {
  isNavigationNode,
  navigationNodeDefaultData,
} from "./nodes/NavigationNode/types";
import {
  extractionNodeDefaultData,
  isExtractionNode,
} from "./nodes/ExtractionNode/types";
import { loginNodeDefaultData } from "./nodes/LoginNode/types";
import { isWaitNode, waitNodeDefaultData } from "./nodes/WaitNode/types";
import { fileDownloadNodeDefaultData } from "./nodes/FileDownloadNode/types";
import { ProxyLocation } from "@/api/types";
import {
  isPdfParserNode,
  pdfParserNodeDefaultData,
} from "./nodes/PDFParserNode/types";
import { taskv2NodeDefaultData } from "./nodes/Taskv2Node/types";
import { urlNodeDefaultData } from "./nodes/URLNode/types";
import { fileUploadNodeDefaultData } from "./nodes/FileUploadNode/types";
export const NEW_NODE_LABEL_PREFIX = "block_";

function layoutUtil(
  nodes: Array<AppNode>,
  edges: Array<Edge>,
  options: Dagre.configUnion = {},
): { nodes: Array<AppNode>; edges: Array<Edge> } {
  const g = new Dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "TB", ...options });

  edges.forEach((edge) => g.setEdge(edge.source, edge.target));
  nodes.forEach((node) =>
    g.setNode(node.id, {
      ...node,
      width: node.measured?.width ?? 0,
      height: node.measured?.height ?? 0,
    }),
  );

  Dagre.layout(g);

  return {
    nodes: nodes.map((node) => {
      const dagreNode = g.node(node.id);
      // We are shifting the dagre node position (anchor=center center) to the top left
      // so it matches the React Flow node anchor point (top left).
      const x = dagreNode.x - (node.measured?.width ?? 0) / 2;
      const y = dagreNode.y - (node.measured?.height ?? 0) / 2;

      return { ...node, position: { x, y } };
    }),
    edges,
  };
}

export function descendants(nodes: Array<AppNode>, id: string): Array<AppNode> {
  const children = nodes.filter((n) => n.parentId === id);
  return children.concat(...children.map((c) => descendants(nodes, c.id)));
}

export function getLoopNodeWidth(node: AppNode, nodes: Array<AppNode>): number {
  const maxNesting = maxNestingLevel(nodes);
  const nestingLevel = getNestingLevel(node, nodes);
  return 600 + (maxNesting - nestingLevel) * 50;
}

function maxNestingLevel(nodes: Array<AppNode>): number {
  return Math.max(...nodes.map((node) => getNestingLevel(node, nodes)));
}

function getNestingLevel(node: AppNode, nodes: Array<AppNode>): number {
  let level = 0;
  let current = nodes.find((n) => n.id === node.parentId);
  while (current) {
    level++;
    current = nodes.find((n) => n.id === current?.parentId);
  }
  return level;
}

function layout(
  nodes: Array<AppNode>,
  edges: Array<Edge>,
): { nodes: Array<AppNode>; edges: Array<Edge> } {
  const loopNodes = nodes.filter((node) => node.type === "loop");
  const loopNodeChildren: Array<Array<AppNode>> = loopNodes.map(() => []);

  loopNodes.forEach((node, index) => {
    const childNodes = nodes.filter((n) => n.parentId === node.id);
    const childEdges = edges.filter((edge) =>
      childNodes.some(
        (node) => node.id === edge.source || node.id === edge.target,
      ),
    );
    const maxChildWidth = Math.max(
      ...childNodes.map((node) => node.measured?.width ?? 0),
    );
    const loopNodeWidth = getLoopNodeWidth(node, nodes);
    const layouted = layoutUtil(childNodes, childEdges, {
      marginx: (loopNodeWidth - maxChildWidth) / 2,
      marginy: 225,
    });
    loopNodeChildren[index] = layouted.nodes;
  });

  const topLevelNodes = nodes.filter((node) => !node.parentId);

  const topLevelNodesLayout = layoutUtil(topLevelNodes, edges);

  return {
    nodes: topLevelNodesLayout.nodes.concat(loopNodeChildren.flat()),
    edges,
  };
}

function convertToNode(
  identifiers: { id: string; parentId?: string },
  block: WorkflowBlock,
  editable: boolean,
): AppNode {
  const common = {
    draggable: false,
    position: { x: 0, y: 0 },
    connectable: false,
  };
  const commonData: NodeBaseData = {
    label: block.label,
    continueOnFailure: block.continue_on_failure,
    editable,
  };
  switch (block.block_type) {
    case "task": {
      return {
        ...identifiers,
        ...common,
        type: "task",
        data: {
          ...commonData,
          url: block.url ?? "",
          navigationGoal: block.navigation_goal ?? "",
          dataExtractionGoal: block.data_extraction_goal ?? "",
          dataSchema: JSON.stringify(block.data_schema, null, 2),
          errorCodeMapping: JSON.stringify(block.error_code_mapping, null, 2),
          allowDownloads: block.complete_on_download ?? false,
          downloadSuffix: block.download_suffix ?? null,
          maxRetries: block.max_retries ?? null,
          maxStepsOverride: block.max_steps_per_run ?? null,
          parameterKeys: block.parameters.map((p) => p.key),
          totpIdentifier: block.totp_identifier ?? null,
          totpVerificationUrl: block.totp_verification_url ?? null,
          cacheActions: block.cache_actions,
          completeCriterion: block.complete_criterion ?? "",
          terminateCriterion: block.terminate_criterion ?? "",
        },
      };
    }
    case "task_v2": {
      return {
        ...identifiers,
        ...common,
        type: "taskv2",
        data: {
          ...commonData,
          prompt: block.prompt,
          url: block.url ?? "",
          maxSteps: block.max_steps,
          totpIdentifier: block.totp_identifier,
          totpVerificationUrl: block.totp_verification_url,
        },
      };
    }
    case "validation": {
      return {
        ...identifiers,
        ...common,
        type: "validation",
        data: {
          ...commonData,
          errorCodeMapping: JSON.stringify(block.error_code_mapping, null, 2),
          completeCriterion: block.complete_criterion ?? "",
          terminateCriterion: block.terminate_criterion ?? "",
          parameterKeys: block.parameters.map((p) => p.key),
        },
      };
    }
    case "action": {
      return {
        ...identifiers,
        ...common,
        type: "action",
        data: {
          ...commonData,
          url: block.url ?? "",
          navigationGoal: block.navigation_goal ?? "",
          errorCodeMapping: JSON.stringify(block.error_code_mapping, null, 2),
          allowDownloads: block.complete_on_download ?? false,
          downloadSuffix: block.download_suffix ?? null,
          maxRetries: block.max_retries ?? null,
          parameterKeys: block.parameters.map((p) => p.key),
          totpIdentifier: block.totp_identifier ?? null,
          totpVerificationUrl: block.totp_verification_url ?? null,
          cacheActions: block.cache_actions,
        },
      };
    }
    case "navigation": {
      return {
        ...identifiers,
        ...common,
        type: "navigation",
        data: {
          ...commonData,
          url: block.url ?? "",
          navigationGoal: block.navigation_goal ?? "",
          errorCodeMapping: JSON.stringify(block.error_code_mapping, null, 2),
          allowDownloads: block.complete_on_download ?? false,
          downloadSuffix: block.download_suffix ?? null,
          maxRetries: block.max_retries ?? null,
          parameterKeys: block.parameters.map((p) => p.key),
          totpIdentifier: block.totp_identifier ?? null,
          totpVerificationUrl: block.totp_verification_url ?? null,
          cacheActions: block.cache_actions,
          maxStepsOverride: block.max_steps_per_run ?? null,
          completeCriterion: block.complete_criterion ?? "",
          terminateCriterion: block.terminate_criterion ?? "",
        },
      };
    }
    case "extraction": {
      return {
        ...identifiers,
        ...common,
        type: "extraction",
        data: {
          ...commonData,
          url: block.url ?? "",
          dataExtractionGoal: block.data_extraction_goal ?? "",
          dataSchema: JSON.stringify(block.data_schema, null, 2),
          parameterKeys: block.parameters.map((p) => p.key),
          maxRetries: block.max_retries ?? null,
          maxStepsOverride: block.max_steps_per_run ?? null,
          cacheActions: block.cache_actions,
        },
      };
    }
    case "login": {
      return {
        ...identifiers,
        ...common,
        type: "login",
        data: {
          ...commonData,
          url: block.url ?? "",
          navigationGoal: block.navigation_goal ?? "",
          errorCodeMapping: JSON.stringify(block.error_code_mapping, null, 2),
          maxRetries: block.max_retries ?? null,
          parameterKeys: block.parameters.map((p) => p.key),
          totpIdentifier: block.totp_identifier ?? null,
          totpVerificationUrl: block.totp_verification_url ?? null,
          cacheActions: block.cache_actions,
          maxStepsOverride: block.max_steps_per_run ?? null,
          completeCriterion: block.complete_criterion ?? "",
          terminateCriterion: block.terminate_criterion ?? "",
        },
      };
    }
    case "wait": {
      return {
        ...identifiers,
        ...common,
        type: "wait",
        data: {
          ...commonData,
          waitInSeconds: String(block.wait_sec ?? 1),
        },
      };
    }
    case "file_download": {
      return {
        ...identifiers,
        ...common,
        type: "fileDownload",
        data: {
          ...commonData,
          url: block.url ?? "",
          navigationGoal: block.navigation_goal ?? "",
          errorCodeMapping: JSON.stringify(block.error_code_mapping, null, 2),
          downloadSuffix: block.download_suffix ?? null,
          maxRetries: block.max_retries ?? null,
          parameterKeys: block.parameters.map((p) => p.key),
          totpIdentifier: block.totp_identifier ?? null,
          totpVerificationUrl: block.totp_verification_url ?? null,
          cacheActions: block.cache_actions,
          maxStepsOverride: block.max_steps_per_run ?? null,
        },
      };
    }
    case "code": {
      return {
        ...identifiers,
        ...common,
        type: "codeBlock",
        data: {
          ...commonData,
          code: block.code,
        },
      };
    }
    case "send_email": {
      return {
        ...identifiers,
        ...common,
        type: "sendEmail",
        data: {
          ...commonData,
          body: block.body,
          fileAttachments: block.file_attachments.join(", "),
          recipients: block.recipients.join(", "),
          subject: block.subject,
          sender: block.sender,
          smtpHostSecretParameterKey: block.smtp_host?.key,
          smtpPortSecretParameterKey: block.smtp_port?.key,
          smtpUsernameSecretParameterKey: block.smtp_username?.key,
          smtpPasswordSecretParameterKey: block.smtp_password?.key,
        },
      };
    }
    case "text_prompt": {
      return {
        ...identifiers,
        ...common,
        type: "textPrompt",
        data: {
          ...commonData,
          prompt: block.prompt,
          jsonSchema: JSON.stringify(block.json_schema, null, 2),
          parameterKeys: block.parameters.map((p) => p.key),
        },
      };
    }
    case "for_loop": {
      const loopVariableReference =
        block.loop_variable_reference !== null
          ? block.loop_variable_reference
          : block.loop_over?.key ?? "";
      return {
        ...identifiers,
        ...common,
        type: "loop",
        data: {
          ...commonData,
          loopValue: block.loop_over?.key ?? "",
          loopVariableReference: loopVariableReference,
          completeIfEmpty: block.complete_if_empty,
        },
      };
    }
    case "file_url_parser": {
      return {
        ...identifiers,
        ...common,
        type: "fileParser",
        data: {
          ...commonData,
          fileUrl: block.file_url,
        },
      };
    }

    case "pdf_parser": {
      return {
        ...identifiers,
        ...common,
        type: "pdfParser",
        data: {
          ...commonData,
          fileUrl: block.file_url,
          jsonSchema: JSON.stringify(block.json_schema, null, 2),
        },
      };
    }

    case "download_to_s3": {
      return {
        ...identifiers,
        ...common,
        type: "download",
        data: {
          ...commonData,
          url: block.url,
        },
      };
    }

    case "upload_to_s3": {
      return {
        ...identifiers,
        ...common,
        type: "upload",
        data: {
          ...commonData,
          path: block.path,
        },
      };
    }

    case "file_upload": {
      return {
        ...identifiers,
        ...common,
        type: "fileUpload",
        data: {
          ...commonData,
          path: block.path,
          storageType: block.storage_type,
          s3Bucket: block.s3_bucket,
          awsAccessKeyId: block.aws_access_key_id,
          awsSecretAccessKey: block.aws_secret_access_key,
          regionName: block.region_name,
        },
      };
    }

    case "goto_url": {
      return {
        ...identifiers,
        ...common,
        type: "url",
        data: {
          ...commonData,
          url: block.url,
        },
      };
    }
  }
}

function generateNodeData(blocks: Array<WorkflowBlock>): Array<{
  id: string;
  previous: string | null;
  next: string | null;
  parentId: string | null;
  block: WorkflowBlock;
}> {
  const idMap = new WeakMap<WorkflowBlock, string>();
  const stack = [...blocks];

  while (stack.length > 0) {
    const block = stack.pop()!;
    const id = nanoid();
    idMap.set(block, id);
    if (block.block_type === "for_loop") {
      stack.push(...block.loop_blocks);
    }
  }

  return getNodeData(blocks, idMap, null);
}

function getNodeData(
  blocks: Array<WorkflowBlock>,
  ids: WeakMap<WorkflowBlock, string>,
  parentId: string | null,
): Array<{
  id: string;
  previous: string | null;
  next: string | null;
  parentId: string | null;
  block: WorkflowBlock;
}> {
  const data: Array<{
    id: string;
    previous: string | null;
    next: string | null;
    parentId: string | null;
    block: WorkflowBlock;
  }> = [];

  blocks.forEach((block, index) => {
    const id = ids.get(block)!;
    const previous = index === 0 ? null : ids.get(blocks[index - 1]!)!;
    const next =
      index === blocks.length - 1 ? null : ids.get(blocks[index + 1]!)!;
    data.push({ id, previous, next, parentId, block });
    if (block.block_type === "for_loop") {
      data.push(...getNodeData(block.loop_blocks, ids, id));
    }
  });

  return data;
}

export function defaultEdge(source: string, target: string) {
  return {
    id: nanoid(),
    type: "default",
    source,
    target,
    style: {
      strokeWidth: 2,
    },
  };
}

export function edgeWithAddButton(source: string, target: string) {
  return {
    id: nanoid(),
    type: "edgeWithAddButton",
    source,
    target,
    style: {
      strokeWidth: 2,
    },
    zIndex: REACT_FLOW_EDGE_Z_INDEX,
  };
}

export function startNode(
  id: string,
  data: StartNodeData,
  parentId?: string,
): StartNode {
  const node: StartNode = {
    id,
    type: "start",
    position: { x: 0, y: 0 },
    data,
    draggable: false,
    connectable: false,
  };
  if (parentId) {
    node.parentId = parentId;
  }
  return node;
}

export function nodeAdderNode(id: string, parentId?: string): NodeAdderNode {
  const node: NodeAdderNode = {
    id,
    type: "nodeAdder",
    position: { x: 0, y: 0 },
    data: {},
    draggable: false,
    connectable: false,
  };
  if (parentId) {
    node.parentId = parentId;
  }
  return node;
}

function getElements(
  blocks: Array<WorkflowBlock>,
  settings: WorkflowSettings,
  editable: boolean,
): {
  nodes: Array<AppNode>;
  edges: Array<Edge>;
} {
  const data = generateNodeData(blocks);
  const nodes: Array<AppNode> = [];
  const edges: Array<Edge> = [];

  const startNodeId = nanoid();
  nodes.push(
    startNode(startNodeId, {
      withWorkflowSettings: true,
      persistBrowserSession: settings.persistBrowserSession,
      proxyLocation: settings.proxyLocation ?? ProxyLocation.Residential,
      webhookCallbackUrl: settings.webhookCallbackUrl ?? "",
      editable,
    }),
  );

  data.forEach((d, index) => {
    const node = convertToNode(
      {
        id: d.id,
        parentId: d.parentId ?? undefined,
      },
      d.block,
      editable,
    );
    nodes.push(node);
    if (d.previous) {
      edges.push(edgeWithAddButton(d.previous, d.id));
    }
    if (index === 0) {
      edges.push(edgeWithAddButton(startNodeId, d.id));
    }
  });

  const loopBlocks = data.filter((d) => d.block.block_type === "for_loop");
  loopBlocks.forEach((block) => {
    const startNodeId = nanoid();
    nodes.push(
      startNode(
        startNodeId,
        {
          withWorkflowSettings: false,
          editable,
        },
        block.id,
      ),
    );
    const children = data.filter((b) => b.parentId === block.id);
    const adderNodeId = nanoid();
    if (children.length === 0) {
      edges.push(defaultEdge(startNodeId, adderNodeId));
    } else {
      const firstChild = children.find((c) => c.previous === null)!;
      edges.push(edgeWithAddButton(startNodeId, firstChild.id));
    }
    const lastChild = children.find((c) => c.next === null);
    nodes.push(nodeAdderNode(adderNodeId, block.id));
    if (lastChild) {
      edges.push(defaultEdge(lastChild.id, adderNodeId));
    }
  });

  const adderNodeId = nanoid();

  if (data.length === 0) {
    nodes.push(nodeAdderNode(adderNodeId));
    edges.push(defaultEdge(startNodeId, adderNodeId));
  } else {
    const lastNode = data.find((d) => d.next === null && d.parentId === null)!;
    edges.push(defaultEdge(lastNode.id, adderNodeId));
    nodes.push(nodeAdderNode(adderNodeId));
  }

  return { nodes, edges };
}

function createNode(
  identifiers: { id: string; parentId?: string },
  nodeType: NonNullable<WorkflowBlockNode["type"]>,
  label: string,
): WorkflowBlockNode {
  const common = {
    draggable: false,
    position: { x: 0, y: 0 },
  };
  switch (nodeType) {
    case "task": {
      return {
        ...identifiers,
        ...common,
        type: "task",
        data: {
          ...taskNodeDefaultData,
          label,
        },
      };
    }
    case "taskv2": {
      return {
        ...identifiers,
        ...common,
        type: "taskv2",
        data: {
          ...taskv2NodeDefaultData,
          label,
        },
      };
    }
    case "validation": {
      return {
        ...identifiers,
        ...common,
        type: "validation",
        data: {
          ...validationNodeDefaultData,
          label,
        },
      };
    }
    case "action": {
      return {
        ...identifiers,
        ...common,
        type: "action",
        data: {
          ...actionNodeDefaultData,
          label,
        },
      };
    }
    case "navigation": {
      return {
        ...identifiers,
        ...common,
        type: "navigation",
        data: {
          ...navigationNodeDefaultData,
          label,
        },
      };
    }
    case "extraction": {
      return {
        ...identifiers,
        ...common,
        type: "extraction",
        data: {
          ...extractionNodeDefaultData,
          label,
        },
      };
    }
    case "login": {
      return {
        ...identifiers,
        ...common,
        type: "login",
        data: {
          ...loginNodeDefaultData,
          label,
        },
      };
    }
    case "wait": {
      return {
        ...identifiers,
        ...common,
        type: "wait",
        data: {
          ...waitNodeDefaultData,
          label,
        },
      };
    }
    case "fileDownload": {
      return {
        ...identifiers,
        ...common,
        type: "fileDownload",
        data: {
          ...fileDownloadNodeDefaultData,
          label,
        },
      };
    }
    case "loop": {
      return {
        ...identifiers,
        ...common,
        type: "loop",
        data: {
          ...loopNodeDefaultData,
          label,
        },
      };
    }
    case "codeBlock": {
      return {
        ...identifiers,
        ...common,
        type: "codeBlock",
        data: {
          ...codeBlockNodeDefaultData,
          label,
        },
      };
    }
    case "download": {
      return {
        ...identifiers,
        ...common,
        type: "download",
        data: {
          ...downloadNodeDefaultData,
          label,
        },
      };
    }
    case "upload": {
      return {
        ...identifiers,
        ...common,
        type: "upload",
        data: {
          ...uploadNodeDefaultData,
          label,
        },
      };
    }
    case "sendEmail": {
      return {
        ...identifiers,
        ...common,
        type: "sendEmail",
        data: {
          ...sendEmailNodeDefaultData,
          label,
        },
      };
    }
    case "textPrompt": {
      return {
        ...identifiers,
        ...common,
        type: "textPrompt",
        data: {
          ...textPromptNodeDefaultData,
          label,
        },
      };
    }
    case "fileParser": {
      return {
        ...identifiers,
        ...common,
        type: "fileParser",
        data: {
          ...fileParserNodeDefaultData,
          label,
        },
      };
    }
    case "pdfParser": {
      return {
        ...identifiers,
        ...common,
        type: "pdfParser",
        data: {
          ...pdfParserNodeDefaultData,
          label,
        },
      };
    }
    case "url": {
      return {
        ...identifiers,
        ...common,
        type: "url",
        data: {
          ...urlNodeDefaultData,
          label,
        },
      };
    }
    case "fileUpload": {
      return {
        ...identifiers,
        ...common,
        type: "fileUpload",
        data: {
          ...fileUploadNodeDefaultData,
          label,
        },
      };
    }
  }
}

function JSONParseSafe(json: string): Record<string, unknown> | null {
  try {
    return JSON.parse(json);
  } catch {
    return null;
  }
}

function getWorkflowBlock(node: WorkflowBlockNode): BlockYAML {
  const base = {
    label: node.data.label,
    continue_on_failure: node.data.continueOnFailure,
  };
  switch (node.type) {
    case "task": {
      return {
        ...base,
        block_type: "task",
        url: node.data.url,
        title: node.data.label,
        navigation_goal: node.data.navigationGoal,
        data_extraction_goal: node.data.dataExtractionGoal,
        complete_criterion: node.data.completeCriterion,
        terminate_criterion: node.data.terminateCriterion,
        data_schema: JSONParseSafe(node.data.dataSchema),
        error_code_mapping: JSONParseSafe(node.data.errorCodeMapping) as Record<
          string,
          string
        > | null,
        ...(node.data.maxRetries !== null && {
          max_retries: node.data.maxRetries,
        }),
        max_steps_per_run: node.data.maxStepsOverride,
        complete_on_download: node.data.allowDownloads,
        download_suffix: node.data.downloadSuffix,
        parameter_keys: node.data.parameterKeys,
        totp_identifier: node.data.totpIdentifier,
        totp_verification_url: node.data.totpVerificationUrl,
        cache_actions: node.data.cacheActions,
      };
    }
    case "taskv2": {
      return {
        ...base,
        block_type: "task_v2",
        prompt: node.data.prompt,
        max_steps: node.data.maxSteps,
        totp_identifier: node.data.totpIdentifier,
        totp_verification_url: node.data.totpVerificationUrl,
        url: node.data.url,
      };
    }
    case "validation": {
      return {
        ...base,
        block_type: "validation",
        complete_criterion: node.data.completeCriterion,
        terminate_criterion: node.data.terminateCriterion,
        error_code_mapping: JSONParseSafe(node.data.errorCodeMapping) as Record<
          string,
          string
        > | null,
        parameter_keys: node.data.parameterKeys,
      };
    }
    case "action": {
      return {
        ...base,
        block_type: "action",
        title: node.data.label,
        navigation_goal: node.data.navigationGoal,
        error_code_mapping: JSONParseSafe(node.data.errorCodeMapping) as Record<
          string,
          string
        > | null,
        url: node.data.url,
        ...(node.data.maxRetries !== null && {
          max_retries: node.data.maxRetries,
        }),
        complete_on_download: node.data.allowDownloads,
        download_suffix: node.data.downloadSuffix,
        parameter_keys: node.data.parameterKeys,
        totp_identifier: node.data.totpIdentifier,
        totp_verification_url: node.data.totpVerificationUrl,
        cache_actions: node.data.cacheActions,
      };
    }
    case "navigation": {
      return {
        ...base,
        block_type: "navigation",
        title: node.data.label,
        navigation_goal: node.data.navigationGoal,
        error_code_mapping: JSONParseSafe(node.data.errorCodeMapping) as Record<
          string,
          string
        > | null,
        url: node.data.url,
        ...(node.data.maxRetries !== null && {
          max_retries: node.data.maxRetries,
        }),
        max_steps_per_run: node.data.maxStepsOverride,
        complete_on_download: node.data.allowDownloads,
        download_suffix: node.data.downloadSuffix,
        parameter_keys: node.data.parameterKeys,
        totp_identifier: node.data.totpIdentifier,
        totp_verification_url: node.data.totpVerificationUrl,
        cache_actions: node.data.cacheActions,
        complete_criterion: node.data.completeCriterion,
        terminate_criterion: node.data.terminateCriterion,
      };
    }
    case "extraction": {
      return {
        ...base,
        block_type: "extraction",
        url: node.data.url,
        title: node.data.label,
        data_extraction_goal: node.data.dataExtractionGoal,
        data_schema: JSONParseSafe(node.data.dataSchema),
        ...(node.data.maxRetries !== null && {
          max_retries: node.data.maxRetries,
        }),
        max_steps_per_run: node.data.maxStepsOverride,
        parameter_keys: node.data.parameterKeys,
        cache_actions: node.data.cacheActions,
      };
    }
    case "login": {
      return {
        ...base,
        block_type: "login",
        title: node.data.label,
        navigation_goal: node.data.navigationGoal,
        error_code_mapping: JSONParseSafe(node.data.errorCodeMapping) as Record<
          string,
          string
        > | null,
        url: node.data.url,
        ...(node.data.maxRetries !== null && {
          max_retries: node.data.maxRetries,
        }),
        max_steps_per_run: node.data.maxStepsOverride,
        parameter_keys: node.data.parameterKeys,
        totp_identifier: node.data.totpIdentifier,
        totp_verification_url: node.data.totpVerificationUrl,
        cache_actions: node.data.cacheActions,
        complete_criterion: node.data.completeCriterion,
        terminate_criterion: node.data.terminateCriterion,
      };
    }
    case "wait": {
      return {
        ...base,
        block_type: "wait",
        wait_sec: Number(node.data.waitInSeconds),
      };
    }
    case "fileDownload": {
      return {
        ...base,
        block_type: "file_download",
        title: node.data.label,
        navigation_goal: node.data.navigationGoal,
        error_code_mapping: JSONParseSafe(node.data.errorCodeMapping) as Record<
          string,
          string
        > | null,
        url: node.data.url,
        ...(node.data.maxRetries !== null && {
          max_retries: node.data.maxRetries,
        }),
        max_steps_per_run: node.data.maxStepsOverride,
        download_suffix: node.data.downloadSuffix,
        parameter_keys: node.data.parameterKeys,
        totp_identifier: node.data.totpIdentifier,
        totp_verification_url: node.data.totpVerificationUrl,
        cache_actions: node.data.cacheActions,
      };
    }
    case "sendEmail": {
      return {
        ...base,
        block_type: "send_email",
        body: node.data.body,
        file_attachments: node.data.fileAttachments
          .split(",")
          .map((attachment) => attachment.trim()),
        recipients: node.data.recipients
          .split(",")
          .map((recipient) => recipient.trim()),
        subject: node.data.subject,
        sender: node.data.sender === "" ? EMAIL_BLOCK_SENDER : node.data.sender,
        smtp_host_secret_parameter_key: node.data.smtpHostSecretParameterKey,
        smtp_port_secret_parameter_key: node.data.smtpPortSecretParameterKey,
        smtp_username_secret_parameter_key:
          node.data.smtpUsernameSecretParameterKey,
        smtp_password_secret_parameter_key:
          node.data.smtpPasswordSecretParameterKey,
      };
    }
    case "codeBlock": {
      return {
        ...base,
        block_type: "code",
        code: node.data.code,
      };
    }
    case "download": {
      return {
        ...base,
        block_type: "download_to_s3",
        url: node.data.url,
      };
    }
    case "upload": {
      return {
        ...base,
        block_type: "upload_to_s3",
        path: node.data.path,
      };
    }
    case "fileUpload": {
      return {
        ...base,
        block_type: "file_upload",
        path: node.data.path,
        storage_type: node.data.storageType,
        s3_bucket: node.data.s3Bucket,
        aws_access_key_id: node.data.awsAccessKeyId,
        aws_secret_access_key: node.data.awsSecretAccessKey,
        region_name: node.data.regionName,
      };
    }
    case "fileParser": {
      return {
        ...base,
        block_type: "file_url_parser",
        file_url: node.data.fileUrl,
        file_type: "csv",
      };
    }
    case "textPrompt": {
      return {
        ...base,
        block_type: "text_prompt",
        llm_key: "",
        prompt: node.data.prompt,
        json_schema: JSONParseSafe(node.data.jsonSchema),
        parameter_keys: node.data.parameterKeys,
      };
    }
    case "pdfParser": {
      return {
        ...base,
        block_type: "pdf_parser",
        file_url: node.data.fileUrl,
        json_schema: JSONParseSafe(node.data.jsonSchema),
      };
    }
    case "url": {
      return {
        ...base,
        block_type: "goto_url",
        url: node.data.url,
      };
    }
    default: {
      throw new Error("Invalid node type for getWorkflowBlock");
    }
  }
}

function getOrderedChildrenBlocks(
  nodes: Array<AppNode>,
  edges: Array<Edge>,
  parentId: string,
): Array<BlockYAML> {
  const parentNode = nodes.find((node) => node.id === parentId);
  if (!parentNode) {
    return [];
  }
  const blockStartNode = nodes.find(
    (node) => node.type === "start" && node.parentId === parentId,
  );
  if (!blockStartNode) {
    return [];
  }
  const firstChildId = edges.find(
    (edge) => edge.source === blockStartNode.id,
  )?.target;
  const firstChild = nodes.find((node) => node.id === firstChildId);
  if (!firstChild || !isWorkflowBlockNode(firstChild)) {
    return [];
  }

  const children: Array<BlockYAML> = [];
  let currentNode: WorkflowBlockNode | undefined = firstChild;
  while (currentNode) {
    if (currentNode.type === "loop") {
      const loopChildren = getOrderedChildrenBlocks(
        nodes,
        edges,
        currentNode.id,
      );
      children.push({
        block_type: "for_loop",
        label: currentNode.data.label,
        continue_on_failure: currentNode.data.continueOnFailure,
        loop_blocks: loopChildren,
        loop_variable_reference: currentNode.data.loopVariableReference,
        complete_if_empty: currentNode.data.completeIfEmpty,
      });
    } else {
      children.push(getWorkflowBlock(currentNode));
    }
    const nextId = edges.find(
      (edge) => edge.source === currentNode?.id,
    )?.target;
    const next = nodes.find((node) => node.id === nextId);
    currentNode = next && isWorkflowBlockNode(next) ? next : undefined;
  }
  return children;
}

function getWorkflowBlocksUtil(
  nodes: Array<AppNode>,
  edges: Array<Edge>,
): Array<BlockYAML> {
  return nodes.flatMap((node) => {
    if (node.parentId || node.type === "start" || node.type === "nodeAdder") {
      return [];
    }
    if (node.type === "loop") {
      return [
        {
          block_type: "for_loop",
          label: node.data.label,
          continue_on_failure: node.data.continueOnFailure,
          loop_blocks: getOrderedChildrenBlocks(nodes, edges, node.id),
          loop_variable_reference: node.data.loopVariableReference,
          complete_if_empty: node.data.completeIfEmpty,
        },
      ];
    }
    return [getWorkflowBlock(node as Exclude<WorkflowBlockNode, LoopNode>)];
  });
}

function getWorkflowBlocks(
  nodes: Array<AppNode>,
  edges: Array<Edge>,
): Array<BlockYAML> {
  return getWorkflowBlocksUtil(nodes, edges);
}

function getWorkflowSettings(nodes: Array<AppNode>): WorkflowSettings {
  const defaultSettings = {
    persistBrowserSession: false,
    proxyLocation: ProxyLocation.Residential,
    webhookCallbackUrl: null,
  };
  const startNodes = nodes.filter(isStartNode);
  const startNodeWithWorkflowSettings = startNodes.find(
    (node) => node.data.withWorkflowSettings,
  );
  if (!startNodeWithWorkflowSettings) {
    return defaultSettings;
  }
  const data = startNodeWithWorkflowSettings.data;
  if (isWorkflowStartNodeData(data)) {
    return {
      persistBrowserSession: data.persistBrowserSession,
      proxyLocation: data.proxyLocation,
      webhookCallbackUrl: data.webhookCallbackUrl,
    };
  }
  return defaultSettings;
}

function generateNodeLabel(existingLabels: Array<string>) {
  for (let i = 1; i < existingLabels.length + 2; i++) {
    const label = NEW_NODE_LABEL_PREFIX + i;
    if (!existingLabels.includes(label)) {
      return label;
    }
  }
  throw new Error("Failed to generate a new node label");
}

/**
 * If a parameter is not displayed in the editor, we should echo its value back when saved.
 */
function convertEchoParameters(
  parameters: Array<AWSSecretParameter>,
): Array<ParameterYAML> {
  return parameters.map((parameter) => {
    if (parameter.parameter_type === "aws_secret") {
      return {
        key: parameter.key,
        parameter_type: "aws_secret",
        aws_key: parameter.aws_key,
      };
    }
    throw new Error("Unknown parameter type");
  });
}

function getOutputParameterKey(label: string) {
  return label + "_output";
}

function isOutputParameterKey(value: string) {
  return value.endsWith("_output");
}

function getBlockNameOfOutputParameterKey(value: string) {
  if (isOutputParameterKey(value)) {
    return value.substring(0, value.length - 7);
  }
  return value;
}

function getUpdatedNodesAfterLabelUpdateForParameterKeys(
  id: string,
  newLabel: string,
  nodes: Array<Node>,
): Array<Node> {
  const labelUpdatedNode = nodes.find((node) => node.id === id);
  if (!labelUpdatedNode) {
    return nodes;
  }
  const oldLabel = labelUpdatedNode.data.label as string;
  return nodes.map((node) => {
    if (node.type === "nodeAdder" || node.type === "start") {
      return node;
    }
    if (node.type === "task" || node.type === "textPrompt") {
      return {
        ...node,
        data: {
          ...node.data,
          parameterKeys: (node.data.parameterKeys as Array<string>).map(
            (key) =>
              key === getOutputParameterKey(oldLabel)
                ? getOutputParameterKey(newLabel)
                : key,
          ),
          label: node.id === id ? newLabel : node.data.label,
        },
      };
    }
    if (node.type === "loop") {
      return {
        ...node,
        data: {
          ...node.data,
          label: node.id === id ? newLabel : node.data.label,
          loopVariableReference:
            node.data.loopVariableReference === getOutputParameterKey(oldLabel)
              ? getOutputParameterKey(newLabel)
              : node.data.loopVariableReference,
        },
      };
    }
    return {
      ...node,
      data: {
        ...node.data,
        label: node.id === id ? newLabel : node.data.label,
      },
    };
  });
}

function getUpdatedParametersAfterLabelUpdateForSourceParameterKey(
  id: string,
  newLabel: string,
  nodes: Array<Node>,
  parameters: ParametersState,
): ParametersState {
  const node = nodes.find((node) => node.id === id);
  if (!node) {
    return parameters;
  }
  const oldLabel = node.data.label as string;
  const oldOutputParameterKey = getOutputParameterKey(oldLabel);
  const newOutputParameterKey = getOutputParameterKey(newLabel);
  return parameters.map((parameter) => {
    if (
      parameter.parameterType === "context" &&
      parameter.sourceParameterKey === oldOutputParameterKey
    ) {
      return {
        ...parameter,
        sourceParameterKey: newOutputParameterKey,
      };
    }
    return parameter;
  });
}

const sendEmailExpectedParameters = [
  {
    key: SMTP_HOST_PARAMETER_KEY,
    aws_key: SMTP_HOST_AWS_KEY,
    parameter_type: WorkflowParameterTypes.AWS_Secret,
  },
  {
    key: SMTP_PORT_PARAMETER_KEY,
    aws_key: SMTP_PORT_AWS_KEY,
    parameter_type: WorkflowParameterTypes.AWS_Secret,
  },
  {
    key: SMTP_USERNAME_PARAMETER_KEY,
    aws_key: SMTP_USERNAME_AWS_KEY,
    parameter_type: WorkflowParameterTypes.AWS_Secret,
  },
  {
    key: SMTP_PASSWORD_PARAMETER_KEY,
    aws_key: SMTP_PASSWORD_AWS_KEY,
    parameter_type: WorkflowParameterTypes.AWS_Secret,
  },
] as const;

function getBlocksOfType(
  blocks: Array<BlockYAML>,
  blockType: WorkflowBlockType,
): Array<BlockYAML> {
  const blocksOfType: Array<BlockYAML> = [];
  for (const block of blocks) {
    if (block.block_type === WorkflowBlockTypes.ForLoop) {
      const subBlocks = block.loop_blocks;
      const subBlocksOfType = getBlocksOfType(subBlocks, blockType);
      blocksOfType.push(...subBlocksOfType);
    } else {
      if (block.block_type === blockType) {
        blocksOfType.push(block);
      }
    }
  }
  return blocksOfType;
}

function getAdditionalParametersForEmailBlock(
  blocks: Array<BlockYAML>,
  parameters: Array<ParameterYAML>,
): Array<ParameterYAML> {
  const emailBlocks = getBlocksOfType(blocks, WorkflowBlockTypes.SendEmail);
  if (emailBlocks.length === 0) {
    return [];
  }
  const sendEmailParameters = sendEmailExpectedParameters.flatMap(
    (parameter) => {
      const existingParameter = parameters.find((p) => p.key === parameter.key);
      if (existingParameter) {
        return [];
      }
      return [parameter];
    },
  );

  return sendEmailParameters;
}

function getUniqueLabelForExistingNode(
  label: string,
  existingLabels: Array<string>,
) {
  if (!existingLabels.includes(label)) {
    return label;
  }
  for (let i = 2; i < existingLabels.length + 1; i++) {
    const candidate = `${label}_${i}`;
    if (!existingLabels.includes(candidate)) {
      return candidate;
    }
  }
  return label;
}

function getDefaultValueForParameterType(
  parameterType: WorkflowParameterValueType,
): unknown {
  switch (parameterType) {
    case "json": {
      return "{}";
    }
    case "string": {
      return "";
    }
    case "boolean": {
      return false;
    }
    case "float":
    case "integer": {
      return 0;
    }
    case "file_url": {
      return null;
    }
    case "credential_id": {
      return null;
    }
  }
}

function getPreviousNodeIds(
  nodes: Array<AppNode>,
  edges: Array<Edge>,
  target: string,
): Array<string> {
  const nodeIds: string[] = [];
  const node = nodes.find((node) => node.id === target);
  if (!node) {
    return nodeIds;
  }
  let current = edges.find((edge) => edge.target === target);
  if (current) {
    while (current) {
      nodeIds.push(current.source);
      current = edges.find((edge) => edge.target === current!.source);
    }
  }
  if (!node.parentId) {
    return nodeIds;
  }
  return [...nodeIds, ...getPreviousNodeIds(nodes, edges, node.parentId)];
}

function getAvailableOutputParameterKeys(
  nodes: Array<AppNode>,
  edges: Array<Edge>,
  id: string,
): Array<string> {
  const previousNodeIds = getPreviousNodeIds(nodes, edges, id);
  const previousNodes = nodes.filter((node) =>
    previousNodeIds.includes(node.id),
  );
  const labels = previousNodes
    .filter(isWorkflowBlockNode)
    .map((node) => node.data.label);
  const outputParameterKeys = labels.map((label) =>
    getOutputParameterKey(label),
  );

  return outputParameterKeys;
}

function convertParametersToParameterYAML(
  parameters: Array<Exclude<Parameter, OutputParameter>>,
): Array<ParameterYAML> {
  return parameters.map((parameter) => {
    const base = {
      key: parameter.key,
      description: parameter.description,
      parameter_type: parameter.parameter_type,
    };
    switch (parameter.parameter_type) {
      case WorkflowParameterTypes.AWS_Secret: {
        return {
          ...base,
          parameter_type: WorkflowParameterTypes.AWS_Secret,
          aws_key: parameter.aws_key,
        };
      }
      case WorkflowParameterTypes.Bitwarden_Login_Credential: {
        return {
          ...base,
          parameter_type: WorkflowParameterTypes.Bitwarden_Login_Credential,
          bitwarden_collection_id: parameter.bitwarden_collection_id,
          bitwarden_item_id: parameter.bitwarden_item_id,
          url_parameter_key: parameter.url_parameter_key,
          bitwarden_client_id_aws_secret_key:
            parameter.bitwarden_client_id_aws_secret_key,
          bitwarden_client_secret_aws_secret_key:
            parameter.bitwarden_client_secret_aws_secret_key,
          bitwarden_master_password_aws_secret_key:
            parameter.bitwarden_master_password_aws_secret_key,
        };
      }
      case WorkflowParameterTypes.Bitwarden_Sensitive_Information: {
        return {
          ...base,
          parameter_type:
            WorkflowParameterTypes.Bitwarden_Sensitive_Information,
          bitwarden_collection_id: parameter.bitwarden_collection_id,
          bitwarden_identity_key: parameter.bitwarden_identity_key,
          bitwarden_identity_fields: parameter.bitwarden_identity_fields,
          bitwarden_client_id_aws_secret_key:
            parameter.bitwarden_client_id_aws_secret_key,
          bitwarden_client_secret_aws_secret_key:
            parameter.bitwarden_client_secret_aws_secret_key,
          bitwarden_master_password_aws_secret_key:
            parameter.bitwarden_master_password_aws_secret_key,
        };
      }
      case WorkflowParameterTypes.Bitwarden_Credit_Card_Data: {
        return {
          ...base,
          parameter_type: WorkflowParameterTypes.Bitwarden_Credit_Card_Data,
          bitwarden_collection_id: parameter.bitwarden_collection_id,
          bitwarden_item_id: parameter.bitwarden_item_id,
          bitwarden_client_id_aws_secret_key:
            parameter.bitwarden_client_id_aws_secret_key,
          bitwarden_client_secret_aws_secret_key:
            parameter.bitwarden_client_secret_aws_secret_key,
          bitwarden_master_password_aws_secret_key:
            parameter.bitwarden_master_password_aws_secret_key,
        };
      }
      case WorkflowParameterTypes.Context: {
        return {
          ...base,
          parameter_type: WorkflowParameterTypes.Context,
          source_parameter_key: parameter.source.key,
        };
      }
      case WorkflowParameterTypes.Workflow: {
        return {
          ...base,
          parameter_type: WorkflowParameterTypes.Workflow,
          workflow_parameter_type: parameter.workflow_parameter_type,
          default_value: parameter.default_value,
        };
      }
      case WorkflowParameterTypes.Credential: {
        return {
          ...base,
          parameter_type: WorkflowParameterTypes.Credential,
          credential_id: parameter.credential_id,
        };
      }
    }
  });
}

function convertBlocksToBlockYAML(
  blocks: Array<WorkflowBlock>,
): Array<BlockYAML> {
  return blocks.map((block) => {
    const base = {
      label: block.label,
      continue_on_failure: block.continue_on_failure,
    };
    switch (block.block_type) {
      case "task": {
        const blockYaml: TaskBlockYAML = {
          ...base,
          block_type: "task",
          title: block.title,
          url: block.url,
          navigation_goal: block.navigation_goal,
          data_extraction_goal: block.data_extraction_goal,
          complete_criterion: block.complete_criterion,
          terminate_criterion: block.terminate_criterion,
          data_schema: block.data_schema,
          error_code_mapping: block.error_code_mapping,
          max_retries: block.max_retries,
          max_steps_per_run: block.max_steps_per_run,
          complete_on_download: block.complete_on_download,
          download_suffix: block.download_suffix,
          parameter_keys: block.parameters.map((p) => p.key),
          totp_identifier: block.totp_identifier,
          totp_verification_url: block.totp_verification_url,
          cache_actions: block.cache_actions,
        };
        return blockYaml;
      }
      case "task_v2": {
        const blockYaml: Taskv2BlockYAML = {
          ...base,
          block_type: "task_v2",
          prompt: block.prompt,
          url: block.url,
          max_steps: block.max_steps,
          totp_identifier: block.totp_identifier,
          totp_verification_url: block.totp_verification_url,
        };
        return blockYaml;
      }
      case "validation": {
        const blockYaml: ValidationBlockYAML = {
          ...base,
          block_type: "validation",
          complete_criterion: block.complete_criterion,
          terminate_criterion: block.terminate_criterion,
          error_code_mapping: block.error_code_mapping,
          parameter_keys: block.parameters.map((p) => p.key),
        };
        return blockYaml;
      }
      case "action": {
        const blockYaml: ActionBlockYAML = {
          ...base,
          block_type: "action",
          url: block.url,
          title: block.title,
          navigation_goal: block.navigation_goal,
          error_code_mapping: block.error_code_mapping,
          max_retries: block.max_retries,
          complete_on_download: block.complete_on_download,
          download_suffix: block.download_suffix,
          parameter_keys: block.parameters.map((p) => p.key),
          totp_identifier: block.totp_identifier,
          totp_verification_url: block.totp_verification_url,
          cache_actions: block.cache_actions,
        };
        return blockYaml;
      }
      case "navigation": {
        const blockYaml: NavigationBlockYAML = {
          ...base,
          block_type: "navigation",
          url: block.url,
          title: block.title,
          navigation_goal: block.navigation_goal,
          error_code_mapping: block.error_code_mapping,
          max_retries: block.max_retries,
          max_steps_per_run: block.max_steps_per_run,
          complete_on_download: block.complete_on_download,
          download_suffix: block.download_suffix,
          parameter_keys: block.parameters.map((p) => p.key),
          totp_identifier: block.totp_identifier,
          totp_verification_url: block.totp_verification_url,
          cache_actions: block.cache_actions,
          complete_criterion: block.complete_criterion,
          terminate_criterion: block.terminate_criterion,
        };
        return blockYaml;
      }
      case "extraction": {
        const blockYaml: ExtractionBlockYAML = {
          ...base,
          block_type: "extraction",
          url: block.url,
          title: block.title,
          data_extraction_goal: block.data_extraction_goal,
          data_schema: block.data_schema,
          max_retries: block.max_retries,
          max_steps_per_run: block.max_steps_per_run,
          parameter_keys: block.parameters.map((p) => p.key),
          cache_actions: block.cache_actions,
        };
        return blockYaml;
      }
      case "login": {
        const blockYaml: LoginBlockYAML = {
          ...base,
          block_type: "login",
          url: block.url,
          title: block.title,
          navigation_goal: block.navigation_goal,
          error_code_mapping: block.error_code_mapping,
          max_retries: block.max_retries,
          max_steps_per_run: block.max_steps_per_run,
          parameter_keys: block.parameters.map((p) => p.key),
          totp_identifier: block.totp_identifier,
          totp_verification_url: block.totp_verification_url,
          cache_actions: block.cache_actions,
          complete_criterion: block.complete_criterion,
          terminate_criterion: block.terminate_criterion,
        };
        return blockYaml;
      }
      case "wait": {
        const blockYaml: WaitBlockYAML = {
          ...base,
          block_type: "wait",
          wait_sec: block.wait_sec,
        };
        return blockYaml;
      }
      case "file_download": {
        const blockYaml: FileDownloadBlockYAML = {
          ...base,
          block_type: "file_download",
          url: block.url,
          title: block.title,
          navigation_goal: block.navigation_goal,
          error_code_mapping: block.error_code_mapping,
          max_retries: block.max_retries,
          max_steps_per_run: block.max_steps_per_run,
          download_suffix: block.download_suffix,
          parameter_keys: block.parameters.map((p) => p.key),
          totp_identifier: block.totp_identifier,
          totp_verification_url: block.totp_verification_url,
          cache_actions: block.cache_actions,
        };
        return blockYaml;
      }
      case "for_loop": {
        const blockYaml: ForLoopBlockYAML = {
          ...base,
          block_type: "for_loop",
          loop_over_parameter_key: block.loop_over?.key ?? "",
          loop_blocks: convertBlocksToBlockYAML(block.loop_blocks),
          loop_variable_reference: block.loop_variable_reference,
          complete_if_empty: block.complete_if_empty,
        };
        return blockYaml;
      }
      case "code": {
        const blockYaml: CodeBlockYAML = {
          ...base,
          block_type: "code",
          code: block.code,
        };
        return blockYaml;
      }
      case "text_prompt": {
        const blockYaml: TextPromptBlockYAML = {
          ...base,
          block_type: "text_prompt",
          llm_key: block.llm_key,
          prompt: block.prompt,
          json_schema: block.json_schema,
          parameter_keys: block.parameters.map((p) => p.key),
        };
        return blockYaml;
      }
      case "download_to_s3": {
        const blockYaml: DownloadToS3BlockYAML = {
          ...base,
          block_type: "download_to_s3",
          url: block.url,
        };
        return blockYaml;
      }
      case "upload_to_s3": {
        const blockYaml: UploadToS3BlockYAML = {
          ...base,
          block_type: "upload_to_s3",
          path: block.path,
        };
        return blockYaml;
      }
      case "file_upload": {
        const blockYaml: FileUploadBlockYAML = {
          ...base,
          block_type: "file_upload",
          path: block.path,
          storage_type: block.storage_type,
          s3_bucket: block.s3_bucket,
          aws_access_key_id: block.aws_access_key_id,
          aws_secret_access_key: block.aws_secret_access_key,
          region_name: block.region_name,
        };
        return blockYaml;
      }
      case "file_url_parser": {
        const blockYaml: FileUrlParserBlockYAML = {
          ...base,
          block_type: "file_url_parser",
          file_url: block.file_url,
          file_type: block.file_type,
        };
        return blockYaml;
      }
      case "pdf_parser": {
        const blockYaml: PDFParserBlockYAML = {
          ...base,
          block_type: "pdf_parser",
          file_url: block.file_url,
          json_schema: block.json_schema,
        };
        return blockYaml;
      }
      case "send_email": {
        const blockYaml: SendEmailBlockYAML = {
          ...base,
          block_type: "send_email",
          smtp_host_secret_parameter_key: block.smtp_host?.key,
          smtp_port_secret_parameter_key: block.smtp_port?.key,
          smtp_username_secret_parameter_key: block.smtp_username?.key,
          smtp_password_secret_parameter_key: block.smtp_password?.key,
          sender: block.sender,
          recipients: block.recipients,
          subject: block.subject,
          body: block.body,
          file_attachments: block.file_attachments,
        };
        return blockYaml;
      }
      case "goto_url": {
        const blockYaml: URLBlockYAML = {
          ...base,
          block_type: "goto_url",
          url: block.url,
        };
        return blockYaml;
      }
    }
  });
}

function convert(workflow: WorkflowApiResponse): WorkflowCreateYAMLRequest {
  const userParameters = workflow.workflow_definition.parameters.filter(
    (parameter) => parameter.parameter_type !== WorkflowParameterTypes.Output,
  );
  return {
    title: workflow.title,
    description: workflow.description,
    proxy_location: workflow.proxy_location,
    webhook_callback_url: workflow.webhook_callback_url,
    totp_verification_url: workflow.totp_verification_url,
    persist_browser_session: workflow.persist_browser_session,
    workflow_definition: {
      parameters: convertParametersToParameterYAML(userParameters),
      blocks: convertBlocksToBlockYAML(workflow.workflow_definition.blocks),
    },
    is_saved_task: workflow.is_saved_task,
  };
}

function getWorkflowErrors(nodes: Array<AppNode>): Array<string> {
  const errors: Array<string> = [];

  const workflowBlockNodes = nodes.filter(isWorkflowBlockNode);
  if (
    workflowBlockNodes.length > 0 &&
    workflowBlockNodes[0]!.type === "validation"
  ) {
    const label = workflowBlockNodes[0]!.data.label;
    errors.push(
      `${label}: Validation block can't be the first block in a workflow.`,
    );
  }

  const actionNodes = nodes.filter(isActionNode);
  actionNodes.forEach((node) => {
    if (node.data.navigationGoal.length === 0) {
      errors.push(`${node.data.label}: Action Instruction is required.`);
    }
    try {
      JSON.parse(node.data.errorCodeMapping);
    } catch {
      errors.push(`${node.data.label}: Error messages is not valid JSON.`);
    }
  });

  // check loop node parameters
  const loopNodes: Array<LoopNode> = nodes.filter(isLoopNode);
  const emptyLoopNodes = loopNodes.filter(
    (node: LoopNode) => node.data.loopVariableReference === "",
  );
  if (emptyLoopNodes.length > 0) {
    emptyLoopNodes.forEach((node) => {
      errors.push(`${node.data.label}: Loop value is required.`);
    });
  }

  // check task node json fields
  const taskNodes = nodes.filter(isTaskNode);
  taskNodes.forEach((node) => {
    try {
      JSON.parse(node.data.dataSchema);
    } catch {
      errors.push(`${node.data.label}: Data schema is not valid JSON.`);
    }
    try {
      JSON.parse(node.data.errorCodeMapping);
    } catch {
      errors.push(`${node.data.label}: Error messages is not valid JSON.`);
    }
  });

  const validationNodes = nodes.filter(isValidationNode);
  validationNodes.forEach((node) => {
    try {
      JSON.parse(node.data.errorCodeMapping);
    } catch {
      errors.push(`${node.data.label}: Error messages is not valid JSON`);
    }
    if (
      node.data.completeCriterion.length === 0 &&
      node.data.terminateCriterion.length === 0
    ) {
      errors.push(
        `${node.data.label}: At least one of completion or termination criteria must be provided`,
      );
    }
  });

  const navigationNodes = nodes.filter(isNavigationNode);
  navigationNodes.forEach((node) => {
    if (node.data.navigationGoal.length === 0) {
      errors.push(`${node.data.label}: Navigation goal is required.`);
    }
  });

  const extractionNodes = nodes.filter(isExtractionNode);
  extractionNodes.forEach((node) => {
    if (node.data.dataExtractionGoal.length === 0) {
      errors.push(`${node.data.label}: Data extraction goal is required.`);
    }
    try {
      JSON.parse(node.data.dataSchema);
    } catch {
      errors.push(`${node.data.label}: Data schema is not valid JSON.`);
    }
  });

  const textPromptNodes = nodes.filter(isTextPromptNode);
  textPromptNodes.forEach((node) => {
    try {
      JSON.parse(node.data.jsonSchema);
    } catch {
      errors.push(`${node.data.label}: Data schema is not valid JSON.`);
    }
  });

  const pdfParserNodes = nodes.filter(isPdfParserNode);
  pdfParserNodes.forEach((node) => {
    try {
      JSON.parse(node.data.jsonSchema);
    } catch {
      errors.push(`${node.data.label}: Data schema is not valid JSON.`);
    }
  });

  const waitNodes = nodes.filter(isWaitNode);
  waitNodes.forEach((node) => {
    const waitTimeString = node.data.waitInSeconds.trim();

    const decimalRegex = new RegExp("^\\d+$");
    const isNumber = decimalRegex.test(waitTimeString);

    if (!isNumber) {
      errors.push(`${node.data.label}: Invalid input for wait time.`);
    }
  });

  return errors;
}

function getLabelForWorkflowParameterType(type: WorkflowParameterValueType) {
  if (type === WorkflowParameterValueType.String) {
    return "string";
  }
  if (type === WorkflowParameterValueType.Float) {
    return "float";
  }
  if (type === WorkflowParameterValueType.Integer) {
    return "integer";
  }
  if (type === WorkflowParameterValueType.Boolean) {
    return "boolean";
  }
  if (type === WorkflowParameterValueType.FileURL) {
    return "file_url";
  }
  if (type === WorkflowParameterValueType.JSON) {
    return "json";
  }
  if (type === WorkflowParameterValueType.CredentialId) {
    return "credential";
  }
  return type;
}

export {
  convert,
  convertEchoParameters,
  createNode,
  generateNodeData,
  generateNodeLabel,
  getNestingLevel,
  getAdditionalParametersForEmailBlock,
  getAvailableOutputParameterKeys,
  getBlockNameOfOutputParameterKey,
  getDefaultValueForParameterType,
  getElements,
  getLabelForWorkflowParameterType,
  maxNestingLevel,
  getWorkflowSettings,
  getOutputParameterKey,
  getPreviousNodeIds,
  getUniqueLabelForExistingNode,
  getUpdatedNodesAfterLabelUpdateForParameterKeys,
  getUpdatedParametersAfterLabelUpdateForSourceParameterKey,
  getWorkflowBlocks,
  getWorkflowErrors,
  isOutputParameterKey,
  layout,
};
