import Dagre from "@dagrejs/dagre";
import { Edge } from "@xyflow/react";
import { nanoid } from "nanoid";
import type {
  WorkflowBlock,
  WorkflowParameterValueType,
} from "../types/workflowTypes";
import { BlockYAML, ParameterYAML } from "../types/workflowYamlTypes";
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
import { AppNode, nodeTypes } from "./nodes";
import { codeBlockNodeDefaultData } from "./nodes/CodeBlockNode/types";
import { downloadNodeDefaultData } from "./nodes/DownloadNode/types";
import { fileParserNodeDefaultData } from "./nodes/FileParserNode/types";
import { LoopNode, loopNodeDefaultData } from "./nodes/LoopNode/types";
import { NodeAdderNode } from "./nodes/NodeAdderNode/types";
import { sendEmailNodeDefaultData } from "./nodes/SendEmailNode/types";
import { taskNodeDefaultData } from "./nodes/TaskNode/types";
import { textPromptNodeDefaultData } from "./nodes/TextPromptNode/types";
import { uploadNodeDefaultData } from "./nodes/UploadNode/types";
import type { Node } from "@xyflow/react";

export const NEW_NODE_LABEL_PREFIX = "Block ";

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
    const loopNodeWidth = 60 * 16; // 60 rem
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
  switch (block.block_type) {
    case "task": {
      return {
        ...identifiers,
        ...common,
        type: "task",
        data: {
          label: block.label,
          editable: true,
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
        },
      };
    }
    case "code": {
      return {
        ...identifiers,
        ...common,
        type: "codeBlock",
        data: {
          label: block.label,
          editable: true,
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
          label: block.label,
          editable: true,
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
          label: block.label,
          editable: true,
          prompt: block.prompt,
          jsonSchema: JSON.stringify(block.json_schema, null, 2),
        },
      };
    }
    case "for_loop": {
      return {
        ...identifiers,
        ...common,
        type: "loop",
        data: {
          label: block.label,
          editable: true,
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
          label: block.label,
          editable: true,
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
          label: block.label,
          editable: true,
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
          label: block.label,
          editable: true,
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

function getElements(blocks: Array<WorkflowBlock>): {
  nodes: Array<AppNode>;
  edges: Array<Edge>;
} {
  const data = generateNodeData(blocks);
  const nodes: Array<AppNode> = [];
  const edges: Array<Edge> = [];

  data.forEach((d) => {
    const node = convertToNode(
      {
        id: d.id,
        parentId: d.parentId ?? undefined,
      },
      d.block,
    );
    nodes.push(node);
    if (d.previous) {
      edges.push({
        id: nanoid(),
        type: "edgeWithAddButton",
        source: d.previous,
        target: d.id,
        style: {
          strokeWidth: 2,
        },
        zIndex: REACT_FLOW_EDGE_Z_INDEX,
      });
    }
  });

  const loopBlocks = data.filter((d) => d.block.block_type === "for_loop");
  loopBlocks.forEach((block) => {
    const children = data.filter((b) => b.parentId === block.id);
    const lastChild = children.find((c) => c.next === null);
    nodes.push({
      id: `${block.id}-nodeAdder`,
      type: "nodeAdder",
      position: { x: 0, y: 0 },
      data: {},
      draggable: false,
      connectable: false,
      parentId: block.id,
    });
    if (lastChild) {
      edges.push({
        id: `${block.id}-nodeAdder-edge`,
        type: "default",
        source: lastChild.id,
        target: `${block.id}-nodeAdder`,
        style: {
          strokeWidth: 2,
        },
      });
    }
  });

  if (nodes.length > 0) {
    const lastNode = data.find((d) => d.next === null && d.parentId === null);
    edges.push({
      id: "edge-nodeAdder",
      type: "default",
      source: lastNode!.id,
      target: "nodeAdder",
      style: {
        strokeWidth: 2,
      },
    });
    nodes.push({
      id: "nodeAdder",
      type: "nodeAdder",
      position: { x: 0, y: 0 },
      data: {},
      draggable: false,
      connectable: false,
    });
  }

  return { nodes, edges };
}

function createNode(
  identifiers: { id: string; parentId?: string },
  nodeType: Exclude<keyof typeof nodeTypes, "nodeAdder">,
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

function getWorkflowBlock(
  node: Exclude<AppNode, LoopNode | NodeAdderNode>,
): BlockYAML {
  switch (node.type) {
    case "task": {
      return {
        block_type: "task",
        label: node.data.label,
        url: node.data.url,
        navigation_goal: node.data.navigationGoal,
        data_extraction_goal: node.data.dataExtractionGoal,
        data_schema: JSONParseSafe(node.data.dataSchema),
        error_code_mapping: JSONParseSafe(node.data.errorCodeMapping) as Record<
          string,
          string
        > | null,
        max_retries: node.data.maxRetries ?? undefined,
        max_steps_per_run: node.data.maxStepsOverride,
        complete_on_download: node.data.allowDownloads,
        download_suffix: node.data.downloadSuffix,
        parameter_keys: node.data.parameterKeys,
        totp_identifier: node.data.totpIdentifier,
        totp_verification_url: node.data.totpVerificationUrl,
      };
    }
    case "sendEmail": {
      return {
        block_type: "send_email",
        label: node.data.label,
        body: node.data.body,
        file_attachments: node.data.fileAttachments.split(","),
        recipients: node.data.recipients.split(","),
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
        block_type: "code",
        label: node.data.label,
        code: node.data.code,
      };
    }
    case "download": {
      return {
        block_type: "download_to_s3",
        label: node.data.label,
        url: node.data.url,
      };
    }
    case "upload": {
      return {
        block_type: "upload_to_s3",
        label: node.data.label,
        path: node.data.path,
      };
    }
    case "fileParser": {
      return {
        block_type: "file_url_parser",
        label: node.data.label,
        file_url: node.data.fileUrl,
        file_type: "csv",
      };
    }
    case "textPrompt": {
      return {
        block_type: "text_prompt",
        label: node.data.label,
        llm_key: "",
        prompt: node.data.prompt,
        json_schema: JSONParseSafe(node.data.jsonSchema),
      };
    }
    default: {
      throw new Error("Invalid node type for getWorkflowBlock");
    }
  }
}

function getWorkflowBlocksUtil(nodes: Array<AppNode>): Array<BlockYAML> {
  return nodes.flatMap((node) => {
    if (node.parentId) {
      return [];
    }
    if (node.type === "loop") {
      return [
        {
          block_type: "for_loop",
          label: node.data.label,
          loop_over_parameter_key: node.data.loopValue,
          loop_blocks: nodes
            .filter((n) => n.parentId === node.id)
            .map((n) => {
              return getWorkflowBlock(
                n as Exclude<AppNode, LoopNode | NodeAdderNode>,
              );
            }),
        },
      ];
    }
    return [
      getWorkflowBlock(node as Exclude<AppNode, LoopNode | NodeAdderNode>),
    ];
  });
}

function getWorkflowBlocks(nodes: Array<AppNode>): Array<BlockYAML> {
  return getWorkflowBlocksUtil(
    nodes.filter((node) => node.type !== "nodeAdder"),
  );
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

import type {
  AWSSecretParameter,
  BitwardenSensitiveInformationParameter,
  ContextParameter,
} from "../types/workflowTypes";

/**
 * If a parameter is not displayed in the editor, we should echo its value back when saved.
 */
function convertEchoParameters(
  parameters: Array<
    | ContextParameter
    | BitwardenSensitiveInformationParameter
    | AWSSecretParameter
  >,
): Array<ParameterYAML> {
  return parameters.map((parameter) => {
    if (parameter.parameter_type === "aws_secret") {
      return {
        key: parameter.key,
        parameter_type: "aws_secret",
        aws_key: parameter.aws_key,
      };
    }
    if (parameter.parameter_type === "bitwarden_sensitive_information") {
      return {
        key: parameter.key,
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
    if (parameter.parameter_type === "context") {
      return {
        key: parameter.key,
        parameter_type: "context",
        source_parameter_key: parameter.source.key,
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
    if (node.type === "task") {
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
    return {
      ...node,
      data: {
        ...node.data,
        label: node.id === id ? newLabel : node.data.label,
      },
    };
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

function getLabelForExistingNode(label: string, existingLabels: Array<string>) {
  if (!existingLabels.includes(label)) {
    return label;
  }
  for (let i = 2; i < existingLabels.length + 1; i++) {
    const candidate = `${label} (${i})`;
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

export {
  createNode,
  generateNodeData,
  getElements,
  getWorkflowBlocks,
  layout,
  generateNodeLabel,
  convertEchoParameters,
  getOutputParameterKey,
  getUpdatedNodesAfterLabelUpdateForParameterKeys,
  getAdditionalParametersForEmailBlock,
  getLabelForExistingNode,
  isOutputParameterKey,
  getBlockNameOfOutputParameterKey,
  getDefaultValueForParameterType,
};
