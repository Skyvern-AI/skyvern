import Dagre from "@dagrejs/dagre";
import type { Node } from "@xyflow/react";
import { Edge } from "@xyflow/react";
import { nanoid } from "nanoid";
import type {
  AWSSecretParameter,
  OutputParameter,
  Parameter,
  WorkflowApiResponse,
  WorkflowBlock,
  WorkflowParameterValueType,
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
import { ParametersState } from "./FlowRenderer";
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
import { StartNode } from "./nodes/StartNode/types";
import { isTaskNode, taskNodeDefaultData } from "./nodes/TaskNode/types";
import { textPromptNodeDefaultData } from "./nodes/TextPromptNode/types";
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

function layout(
  nodes: Array<AppNode>,
  edges: Array<Edge>,
): { nodes: Array<AppNode>; edges: Array<Edge> } {
  const loopNodes = nodes.filter(
    (node) => node.type === "loop" && !node.parentId,
  );
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
    const loopNodeWidth = 600; // 600 px
    const layouted = layoutUtil(childNodes, childEdges, {
      marginx: (loopNodeWidth - maxChildWidth) / 2,
      marginy: 200,
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
): AppNode {
  const common = {
    draggable: false,
    position: { x: 0, y: 0 },
    connectable: false,
  };
  const commonData: NodeBaseData = {
    label: block.label,
    continueOnFailure: block.continue_on_failure,
    editable: true,
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
      return {
        ...identifiers,
        ...common,
        type: "loop",
        data: {
          ...commonData,
          loopValue: block.loop_over.key,
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

export function startNode(id: string, parentId?: string): StartNode {
  const node: StartNode = {
    id,
    type: "start",
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

function getElements(blocks: Array<WorkflowBlock>): {
  nodes: Array<AppNode>;
  edges: Array<Edge>;
} {
  const data = generateNodeData(blocks);
  const nodes: Array<AppNode> = [];
  const edges: Array<Edge> = [];

  const startNodeId = nanoid();
  nodes.push(startNode(startNodeId));

  data.forEach((d, index) => {
    const node = convertToNode(
      {
        id: d.id,
        parentId: d.parentId ?? undefined,
      },
      d.block,
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
    nodes.push(startNode(startNodeId, block.id));
    const children = data.filter((b) => b.parentId === block.id);
    if (children.length === 0) {
      const adderNodeId = nanoid();
      nodes.push(nodeAdderNode(adderNodeId, block.id));
      edges.push(defaultEdge(startNodeId, adderNodeId));
    } else {
      const firstChild = children.find((c) => c.previous === null)!;
      edges.push(edgeWithAddButton(startNodeId, firstChild.id));
    }
    const lastChild = children.find((c) => c.next === null);
    const adderNodeId = nanoid();
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
    const firstNode = data.find(
      (d) => d.previous === null && d.parentId === null,
    );
    edges.push(edgeWithAddButton(startNodeId, firstNode!.id));
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
): AppNode {
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
    children.push(getWorkflowBlock(currentNode));
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
          loop_over_parameter_key: node.data.loopValue,
          loop_blocks: getOrderedChildrenBlocks(nodes, edges, node.id),
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
          loopValue:
            node.data.loopValue === getOutputParameterKey(oldLabel)
              ? getOutputParameterKey(newLabel)
              : node.data.loopValue,
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
    parameter_type: "aws_secret",
  },
  {
    key: SMTP_PORT_PARAMETER_KEY,
    aws_key: SMTP_PORT_AWS_KEY,
    parameter_type: "aws_secret",
  },
  {
    key: SMTP_USERNAME_PARAMETER_KEY,
    aws_key: SMTP_USERNAME_AWS_KEY,
    parameter_type: "aws_secret",
  },
  {
    key: SMTP_PASSWORD_PARAMETER_KEY,
    aws_key: SMTP_PASSWORD_AWS_KEY,
    parameter_type: "aws_secret",
  },
] as const;

function getAdditionalParametersForEmailBlock(
  blocks: Array<BlockYAML>,
  parameters: Array<ParameterYAML>,
): Array<ParameterYAML> {
  const emailBlocks = blocks.filter(
    (block) => block.block_type === "send_email",
  );
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
    };
    switch (parameter.parameter_type) {
      case "aws_secret": {
        return {
          ...base,
          parameter_type: "aws_secret",
          aws_key: parameter.aws_key,
        };
      }
      case "bitwarden_login_credential": {
        return {
          ...base,
          parameter_type: "bitwarden_login_credential",
          bitwarden_collection_id: parameter.bitwarden_collection_id,
          url_parameter_key: parameter.url_parameter_key,
          bitwarden_client_id_aws_secret_key: "SKYVERN_BITWARDEN_CLIENT_ID",
          bitwarden_client_secret_aws_secret_key:
            "SKYVERN_BITWARDEN_CLIENT_SECRET",
          bitwarden_master_password_aws_secret_key:
            "SKYVERN_BITWARDEN_MASTER_PASSWORD",
        };
      }
      case "bitwarden_sensitive_information": {
        return {
          ...base,
          parameter_type: "bitwarden_sensitive_information",
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
      case "context": {
        return {
          ...base,
          parameter_type: "context",
          source_parameter_key: parameter.source.key,
        };
      }
      case "workflow": {
        return {
          ...base,
          parameter_type: "workflow",
          workflow_parameter_type: parameter.workflow_parameter_type,
          default_value: parameter.default_value,
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
      case "for_loop": {
        const blockYaml: ForLoopBlockYAML = {
          ...base,
          block_type: "for_loop",
          loop_over_parameter_key: block.loop_over.key,
          loop_blocks: convertBlocksToBlockYAML(block.loop_blocks),
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
      case "file_url_parser": {
        const blockYaml: FileUrlParserBlockYAML = {
          ...base,
          block_type: "file_url_parser",
          file_url: block.file_url,
          file_type: block.file_type,
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
    }
  });
}

function convert(workflow: WorkflowApiResponse): WorkflowCreateYAMLRequest {
  const userParameters = workflow.workflow_definition.parameters.filter(
    (parameter) => parameter.parameter_type !== "output",
  );
  return {
    title: workflow.title,
    description: workflow.description,
    proxy_location: workflow.proxy_location,
    webhook_callback_url: workflow.webhook_callback_url,
    totp_verification_url: workflow.totp_verification_url,
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
    (node: LoopNode) => node.data.loopValue === "",
  );
  if (emptyLoopNodes.length > 0) {
    emptyLoopNodes.forEach((node) => {
      errors.push(`${node.data.label}: Loop value parameter must be selected.`);
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
  });

  return errors;
}

export {
  convert,
  convertEchoParameters,
  createNode,
  generateNodeData,
  generateNodeLabel,
  getAdditionalParametersForEmailBlock,
  getAvailableOutputParameterKeys,
  getBlockNameOfOutputParameterKey,
  getDefaultValueForParameterType,
  getElements,
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
