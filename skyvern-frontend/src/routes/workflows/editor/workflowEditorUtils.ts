import { Edge } from "@xyflow/react";
import { AppNode } from "./nodes";
import Dagre from "@dagrejs/dagre";
import type { WorkflowBlock } from "../types/workflowTypes";
import { nodeTypes } from "./nodes";
import { taskNodeDefaultData } from "./nodes/TaskNode/types";
import { LoopNode, loopNodeDefaultData } from "./nodes/LoopNode/types";
import { codeBlockNodeDefaultData } from "./nodes/CodeBlockNode/types";
import { downloadNodeDefaultData } from "./nodes/DownloadNode/types";
import { uploadNodeDefaultData } from "./nodes/UploadNode/types";
import { sendEmailNodeDefaultData } from "./nodes/SendEmailNode/types";
import { textPromptNodeDefaultData } from "./nodes/TextPromptNode/types";
import { fileParserNodeDefaultData } from "./nodes/FileParserNode/types";
import { BlockYAML } from "../types/workflowYamlTypes";
import { NodeAdderNode } from "./nodes/NodeAdderNode/types";
import { REACT_FLOW_EDGE_Z_INDEX } from "./constants";

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
          maxRetries: block.max_retries ?? null,
          maxStepsOverride: block.max_steps_per_run ?? null,
          parameterKeys: block.parameters.map((p) => p.key),
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

function getElements(
  blocks: Array<WorkflowBlock>,
  parentId?: string,
): { nodes: Array<AppNode>; edges: Array<Edge> } {
  const nodes: Array<AppNode> = [];
  const edges: Array<Edge> = [];

  blocks.forEach((block, index) => {
    const id = parentId ? `${parentId}-${index}` : String(index);
    const nextId = parentId ? `${parentId}-${index + 1}` : String(index + 1);
    nodes.push(convertToNode({ id, parentId }, block));
    if (block.block_type === "for_loop") {
      const subElements = getElements(block.loop_blocks, id);
      if (subElements.nodes.length === 0) {
        nodes.push({
          id: `${id}-nodeAdder`,
          type: "nodeAdder",
          position: { x: 0, y: 0 },
          data: {},
          draggable: false,
          connectable: false,
        });
      }
      nodes.push(...subElements.nodes);
      edges.push(...subElements.edges);
    }
    if (index !== blocks.length - 1) {
      edges.push({
        id: `edge-${id}-${nextId}`,
        type: "edgeWithAddButton",
        source: id,
        target: nextId,
        style: {
          strokeWidth: 2,
        },
        zIndex: REACT_FLOW_EDGE_Z_INDEX,
      });
    }
  });

  if (nodes.length > 0) {
    edges.push({
      id: "edge-nodeAdder",
      type: "default",
      source: nodes[nodes.length - 1]!.id,
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
  labelPostfix: string, // unique label requirement
): AppNode {
  const label = "Block " + labelPostfix;
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
        parameter_keys: node.data.parameterKeys,
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
        sender: node.data.sender,
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

export { getElements, layout, createNode, getWorkflowBlocks };
