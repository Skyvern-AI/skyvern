import { Edge } from "@xyflow/react";
import { AppNode } from "./nodes";
import Dagre from "@dagrejs/dagre";
import type { WorkflowBlock } from "../types/workflowTypes";

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
    const layouted = layoutUtil(childNodes, childEdges, {
      marginx: 240,
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
  };
  switch (block.block_type) {
    case "task": {
      return {
        ...identifiers,
        ...common,
        type: "task",
        data: {
          label: block.label,
          editable: false,
          url: block.url ?? "",
          navigationGoal: block.navigation_goal ?? "",
          dataExtractionGoal: block.data_extraction_goal ?? "",
          dataSchema: block.data_schema ?? null,
          errorCodeMapping: block.error_code_mapping ?? null,
          allowDownloads: block.complete_on_download ?? false,
          maxRetries: block.max_retries ?? null,
          maxStepsOverride: block.max_steps_per_run ?? null,
        },
        position: { x: 0, y: 0 },
      };
    }
    case "code": {
      return {
        ...identifiers,
        ...common,
        type: "codeBlock",
        data: {
          label: block.label,
          editable: false,
          code: block.code,
        },
        position: { x: 0, y: 0 },
      };
    }
    case "send_email": {
      return {
        ...identifiers,
        ...common,
        type: "sendEmail",
        data: {
          label: block.label,
          editable: false,
          body: block.body,
          fileAttachments: block.file_attachments,
          recipients: block.recipients,
          subject: block.subject,
        },
        position: { x: 0, y: 0 },
      };
    }
    case "text_prompt": {
      return {
        ...identifiers,
        ...common,
        type: "textPrompt",
        data: {
          label: block.label,
          editable: false,
          prompt: block.prompt,
          jsonSchema: block.json_schema ?? null,
        },
        position: { x: 0, y: 0 },
      };
    }
    case "for_loop": {
      return {
        ...identifiers,
        ...common,
        type: "loop",
        data: {
          label: block.label,
          editable: false,
          loopValue: block.loop_over.key,
        },
        position: { x: 0, y: 0 },
      };
    }
    case "file_url_parser": {
      return {
        ...identifiers,
        ...common,
        type: "fileParser",
        data: {
          label: block.label,
          editable: false,
          fileUrl: block.file_url,
        },
        position: { x: 0, y: 0 },
      };
    }

    case "download_to_s3": {
      return {
        ...identifiers,
        ...common,
        type: "download",
        data: {
          label: block.label,
          editable: false,
          url: block.url,
        },
        position: { x: 0, y: 0 },
      };
    }

    case "upload_to_s3": {
      return {
        ...identifiers,
        ...common,
        type: "upload",
        data: {
          label: block.label,
          editable: false,
          path: block.path,
        },
        position: { x: 0, y: 0 },
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
      nodes.push(...subElements.nodes);
      edges.push(...subElements.edges);
    }
    if (index !== blocks.length - 1) {
      edges.push({
        id: `edge-${id}-${nextId}`,
        source: id,
        target: nextId,
        style: {
          strokeWidth: 2,
        },
      });
    }
  });

  return { nodes, edges };
}

export { getElements, layout };
