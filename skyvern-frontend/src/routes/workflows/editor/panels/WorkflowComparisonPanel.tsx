import { useCallback, useMemo } from "react";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  ReactFlowProvider,
  useNodesState,
  useEdgesState,
  NodeChange,
  EdgeChange,
} from "@xyflow/react";
import { WorkflowVersion } from "../../hooks/useWorkflowVersionsQuery";
import { WorkflowBlock, WorkflowSettings } from "../../types/workflowTypes";
import { FlowRenderer } from "../FlowRenderer";
import { getElements } from "../workflowEditorUtils";
import { ProxyLocation } from "@/api/types";
import { AppNode } from "../nodes";

type BlockComparison = {
  leftBlock?: WorkflowBlock;
  rightBlock?: WorkflowBlock;
  status: "identical" | "modified" | "added" | "removed";
  identifier: string;
};

type Props = {
  version1: WorkflowVersion;
  version2: WorkflowVersion;
  onSelectState?: (version: WorkflowVersion) => void;
};

// Mapping from WorkflowBlock.block_type to ReactFlow node.type
const BLOCK_TYPE_TO_NODE_TYPE: Record<string, string> = {
  task: "task",
  task_v2: "taskv2",
  validation: "validation",
  action: "action",
  navigation: "navigation",
  extraction: "extraction",
  login: "login",
  wait: "wait",
  file_download: "fileDownload",
  code: "codeBlock",
  send_email: "sendEmail",
  text_prompt: "textPrompt",
  for_loop: "loop",
  file_url_parser: "fileParser",
  pdf_parser: "pdfParser",
  download_to_s3: "download",
  upload_to_s3: "upload",
  file_upload: "fileUpload",
  goto_url: "url",
  http_request: "http_request",
};

function getBlockIdentifier(block: WorkflowBlock): string {
  // Convert block_type to node type for consistent comparison
  const nodeType =
    BLOCK_TYPE_TO_NODE_TYPE[block.block_type] || block.block_type;
  return `${nodeType}:${block.label}`;
}

function areBlocksIdentical(
  block1: WorkflowBlock,
  block2: WorkflowBlock,
): boolean {
  // Convert blocks to string representation for comparison
  // Remove dynamic fields that shouldn't affect equality
  const normalize = (block: WorkflowBlock) => {
    const normalized = { ...block };
    // Remove output_parameter as it might have different IDs
    const { output_parameter, ...rest } = normalized;
    console.log(output_parameter);
    return JSON.stringify(rest, Object.keys(rest).sort());
  };

  return normalize(block1) === normalize(block2);
}

function compareWorkflowBlocks(
  blocks1: WorkflowBlock[],
  blocks2: WorkflowBlock[],
): BlockComparison[] {
  const comparisons: BlockComparison[] = [];
  const processedBlocks = new Set<string>();

  // Create maps for quick lookup
  const blocks1Map = new Map<string, WorkflowBlock>();
  const blocks2Map = new Map<string, WorkflowBlock>();

  blocks1.forEach((block) => {
    const identifier = getBlockIdentifier(block);
    blocks1Map.set(identifier, block);
  });

  blocks2.forEach((block) => {
    const identifier = getBlockIdentifier(block);
    blocks2Map.set(identifier, block);
  });

  // Compare blocks that exist in the first version
  blocks1.forEach((block1) => {
    const identifier = getBlockIdentifier(block1);
    const block2 = blocks2Map.get(identifier);
    processedBlocks.add(identifier);

    if (block2) {
      // Block exists in both versions
      const isIdentical = areBlocksIdentical(block1, block2);
      comparisons.push({
        leftBlock: block1,
        rightBlock: block2,
        status: isIdentical ? "identical" : "modified",
        identifier,
      });
    } else {
      // Block was removed in version 2
      comparisons.push({
        leftBlock: block1,
        rightBlock: undefined,
        status: "removed",
        identifier,
      });
    }
  });

  // Check for blocks that were added in version 2
  blocks2.forEach((block2) => {
    const identifier = getBlockIdentifier(block2);
    if (!processedBlocks.has(identifier)) {
      comparisons.push({
        leftBlock: undefined,
        rightBlock: block2,
        status: "added",
        identifier,
      });
    }
  });

  return comparisons;
}

function getWorkflowElements(version: WorkflowVersion) {
  const settings: WorkflowSettings = {
    proxyLocation: version.proxy_location || ProxyLocation.Residential,
    webhookCallbackUrl: version.webhook_callback_url || "",
    persistBrowserSession: version.persist_browser_session,
    model: version.model,
    maxScreenshotScrolls: version.max_screenshot_scrolls || 3,
    extraHttpHeaders: version.extra_http_headers
      ? JSON.stringify(version.extra_http_headers)
      : null,
    runWith: version.run_with,
    scriptCacheKey: version.cache_key,
    aiFallback: version.ai_fallback ?? true,
    runSequentially: version.run_sequentially ?? false,
    sequentialKey: version.sequential_key ?? null,
  };

  // Deep clone the blocks to ensure complete isolation from main editor
  const blocks = JSON.parse(
    JSON.stringify(version.workflow_definition?.blocks || []),
  );

  return getElements(
    blocks,
    settings,
    false, // not editable in comparison view
  );
}

function WorkflowComparisonRenderer({
  version,
  blockColors,
}: {
  version: WorkflowVersion;
  onSelectState?: (version: WorkflowVersion) => void;
  blockColors?: Map<string, string>;
}) {
  // Memoize elements creation to prevent unnecessary re-renders
  const elements = useMemo(() => getWorkflowElements(version), [version]);

  // Memoize the colored nodes to prevent re-computation
  const coloredNodes = useMemo(() => {
    if (!blockColors || blockColors.size === 0) {
      return elements.nodes;
    }

    // Apply comparison colors to block nodes
    return elements.nodes.map((node) => {
      // Check if this is a workflow block node (not start/nodeAdder)
      if (
        node.type !== "nodeAdder" &&
        node.type !== "start" &&
        node.data &&
        node.data.label
      ) {
        // This is a workflow block node - get its identifier and color
        const identifier = `${node.type}:${node.data.label}`;
        const color = blockColors.get(identifier);

        if (color) {
          return {
            ...node,
            data: {
              ...node.data,
              comparisonColor: color,
            },
            style: {
              ...node.style,
              backgroundColor: color,
              border: `2px solid ${color}`,
            },
          };
        }
      }
      return node;
    });
  }, [elements.nodes, blockColors]);

  const [nodes, setNodes, onNodesChange] = useNodesState(
    coloredNodes as AppNode[],
  );
  const [edges, setEdges, onEdgesChange] = useEdgesState(elements.edges);

  const handleNodesChange = useCallback(
    (changes: NodeChange<AppNode>[]) => {
      onNodesChange(changes);
    },
    [onNodesChange],
  );

  const handleEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      onEdgesChange(changes);
    },
    [onEdgesChange],
  );

  return (
    <div className="h-full w-full rounded-lg border bg-white">
      <FlowRenderer
        hideBackground={false}
        hideControls={true}
        nodes={nodes}
        edges={edges}
        setNodes={setNodes}
        setEdges={setEdges}
        onNodesChange={handleNodesChange}
        onEdgesChange={handleEdgesChange}
        initialTitle={version.title}
        workflow={version}
      />
    </div>
  );
}

function WorkflowComparisonPanel({ version1, version2, onSelectState }: Props) {
  const comparisons = useMemo(() => {
    const blocks1 = version1?.workflow_definition?.blocks || [];
    const blocks2 = version2?.workflow_definition?.blocks || [];
    return compareWorkflowBlocks(blocks1, blocks2);
  }, [
    version1?.workflow_definition?.blocks,
    version2?.workflow_definition?.blocks,
  ]);

  // Statistics
  const stats = useMemo(
    () => ({
      identical: comparisons.filter((c) => c.status === "identical").length,
      modified: comparisons.filter((c) => c.status === "modified").length,
      added: comparisons.filter((c) => c.status === "added").length,
      removed: comparisons.filter((c) => c.status === "removed").length,
    }),
    [comparisons],
  );

  // Create color mapping for block identifiers
  const getComparisonColor = (
    status: "identical" | "modified" | "added" | "removed",
  ): string => {
    switch (status) {
      case "identical":
        return "#86efac"; // green-300
      case "modified":
        return "#facc15"; // yellow-400
      case "added":
      case "removed":
        return "#c2410c"; // orange-700
      default:
        return "";
    }
  };

  // Create memoized maps for each version's block colors
  const { version1BlockColors, version2BlockColors } = useMemo(() => {
    const v1Colors = new Map<string, string>();
    const v2Colors = new Map<string, string>();

    comparisons.forEach((comparison) => {
      const color = getComparisonColor(comparison.status);

      // For version1 blocks
      if (comparison.leftBlock) {
        v1Colors.set(comparison.identifier, color);
      }

      // For version2 blocks
      if (comparison.rightBlock) {
        v2Colors.set(comparison.identifier, color);
      }
    });

    return {
      version1BlockColors: v1Colors,
      version2BlockColors: v2Colors,
    };
  }, [comparisons]);

  return (
    <div className="flex h-full w-full flex-col rounded-lg bg-slate-elevation2">
      {/* Header */}
      <div className="flex-shrink-0 p-4 pb-3">
        {/* 3x3 Grid Layout */}
        <div className="grid grid-cols-3 gap-4">
          {/* Row 1: Workflow Names and Title */}
          <h2 className="text-center text-xl font-semibold">
            {version1.title}
          </h2>
          <h3 className="text-center text-lg font-medium text-muted-foreground">
            Version Comparison
          </h3>
          <h2 className="text-center text-xl font-semibold">
            {version2.title}
          </h2>

          {/* Row 2: Version Details and Statistics */}
          <div className="text-center text-sm text-muted-foreground">
            [Version {version1.version}] •{" "}
            {new Date(version1.modified_at).toLocaleDateString()}
          </div>
          <div className="flex justify-center gap-3 text-sm">
            <div className="flex items-center gap-1">
              <div className="h-3 w-3 rounded-full bg-green-300"></div>
              <span>Identical ({stats.identical})</span>
            </div>
            <div className="flex items-center gap-1">
              <div className="h-3 w-3 rounded-full bg-yellow-400"></div>
              <span>Modified ({stats.modified})</span>
            </div>
            <div className="flex items-center gap-1">
              <div className="h-3 w-3 rounded-full bg-orange-700"></div>
              <span>Added ({stats.added})</span>
            </div>
            <div className="flex items-center gap-1">
              <div className="h-3 w-3 rounded-full bg-orange-700"></div>
              <span>Removed ({stats.removed})</span>
            </div>
          </div>
          <div className="text-center text-sm text-muted-foreground">
            [Version {version2.version}] •{" "}
            {new Date(version2.modified_at).toLocaleDateString()}
          </div>

          {/* Row 3: Select Buttons */}
          <div className="flex justify-center">
            {onSelectState && (
              <Button
                size="sm"
                onClick={() => onSelectState(version1)}
                className="text-xs"
              >
                Select this variant
              </Button>
            )}
          </div>
          <div></div>
          <div className="flex justify-center">
            {onSelectState && (
              <Button
                size="sm"
                onClick={() => onSelectState(version2)}
                className="text-xs"
              >
                Select this variant
              </Button>
            )}
          </div>
        </div>
      </div>

      <Separator />

      {/* Content - Two columns for comparison */}
      <div className="flex-1 overflow-hidden p-4">
        <div className="grid h-full grid-cols-2 gap-4">
          {/* Version 1 Column */}
          <ReactFlowProvider>
            <WorkflowComparisonRenderer
              key={`k1-${version1.workflow_id}v${version1.version}`}
              version={version1}
              onSelectState={onSelectState}
              blockColors={version1BlockColors}
            />
          </ReactFlowProvider>

          {/* Version 2 Column */}
          <ReactFlowProvider>
            <WorkflowComparisonRenderer
              key={`k2-${version2.workflow_id}v${version2.version}`}
              version={version2}
              onSelectState={onSelectState}
              blockColors={version2BlockColors}
            />
          </ReactFlowProvider>
        </div>
      </div>
    </div>
  );
}

export { WorkflowComparisonPanel };
