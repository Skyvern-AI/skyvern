import { useCallback, useEffect, useMemo } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Cross1Icon } from "@radix-ui/react-icons";
import {
  ReactFlowProvider,
  useNodesState,
  useEdgesState,
  NodeChange,
  EdgeChange,
} from "@xyflow/react";
import { WorkflowVersion } from "../hooks/useWorkflowVersionsQuery";
import { WorkflowBlock, WorkflowSettings } from "../types/workflowTypes";
import { FlowRenderer } from "../editor/FlowRenderer";
import { getElements } from "../editor/workflowEditorUtils";
import { ProxyLocation } from "@/api/types";
import { AppNode } from "../editor/nodes";
import { areBlocksIdentical } from "../util/compareBlocks";

type BlockComparison = {
  leftBlock?: WorkflowBlock;
  rightBlock?: WorkflowBlock;
  status: "identical" | "modified" | "added" | "removed";
  identifier: string;
};

type Props = {
  version1: WorkflowVersion;
  version2: WorkflowVersion;
  isOpen: boolean;
  onClose: () => void;
};

function getBlockIdentifier(block: WorkflowBlock): string {
  return `${block.block_type}:${block.label}`;
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
    proxyLocation: version.proxy_location ?? ProxyLocation.Residential,
    webhookCallbackUrl: version.webhook_callback_url || "",
    persistBrowserSession: version.persist_browser_session,
    browserProfileId: version.browser_profile_id ?? null,
    model: version.model,
    maxScreenshotScrolls: version.max_screenshot_scrolls || 3,
    extraHttpHeaders: version.extra_http_headers
      ? JSON.stringify(version.extra_http_headers)
      : null,
    cdpConnectHeaders: version.cdp_connect_headers
      ? JSON.stringify(version.cdp_connect_headers)
      : null,
    runWith: version.run_with ?? "agent",
    codeVersion: version.code_version ?? null,
    scriptCacheKey: version.cache_key,
    aiFallback: version.ai_fallback ?? true,
    runSequentially: version.run_sequentially ?? false,
    sequentialKey: version.sequential_key ?? null,
    finallyBlockLabel: version.workflow_definition?.finally_block_label ?? null,
    workflowSystemPrompt:
      version.workflow_definition?.workflow_system_prompt ?? null,
  };

  return getElements(
    version.workflow_definition?.blocks || [],
    settings,
    false, // not editable in comparison view
  );
}

function WorkflowComparisonRenderer({
  version,
  blockColors,
}: {
  version: WorkflowVersion;
  title: string;
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

  // useNodesState only reads its initial argument; re-sync when colored nodes
  // change so "modified" highlights show up after blockColors settles.
  useEffect(() => {
    setNodes(coloredNodes as AppNode[]);
  }, [coloredNodes, setNodes]);

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
    <div className="h-full w-full">
      <div className="mb-4 flex items-center justify-center">
        <div className="text-center">
          <div className="mb-1 flex items-center justify-center gap-2">
            <Badge variant="secondary">
              {version.title}, version: {version.version}
            </Badge>
            <Badge variant="secondary">
              {version.workflow_definition?.blocks?.length || 0} block
              {(version.workflow_definition?.blocks?.length || 0) !== 1
                ? "s"
                : ""}
            </Badge>
          </div>
        </div>
      </div>
      <div className="h-[calc(100%-3rem)] rounded-lg border bg-card">
        <FlowRenderer
          hideBackground={false}
          readOnly
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
    </div>
  );
}

function WorkflowVisualComparisonDrawer({
  version1,
  version2,
  isOpen,
  onClose,
}: Props) {
  const comparisons = useMemo(() => {
    const blocks1 = version1.workflow_definition?.blocks || [];
    const blocks2 = version2.workflow_definition?.blocks || [];
    return compareWorkflowBlocks(blocks1, blocks2);
  }, [version1.workflow_definition, version2.workflow_definition]);

  // Statistics
  const stats = {
    identical: comparisons.filter((c) => c.status === "identical").length,
    modified: comparisons.filter((c) => c.status === "modified").length,
    added: comparisons.filter((c) => c.status === "added").length,
    removed: comparisons.filter((c) => c.status === "removed").length,
  };

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

  // Memoize so the child's setNodes effect doesn't re-fire every render.
  const { version1BlockColors, version2BlockColors } = useMemo(() => {
    const v1 = new Map<string, string>();
    const v2 = new Map<string, string>();
    comparisons.forEach((comparison) => {
      const color = getComparisonColor(comparison.status);
      if (comparison.leftBlock) {
        v1.set(comparison.identifier, color);
      }
      if (comparison.rightBlock) {
        v2.set(comparison.identifier, color);
      }
    });
    return { version1BlockColors: v1, version2BlockColors: v2 };
  }, [comparisons]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex bg-black/50">
      {/* Main Drawer */}
      <div className="bg-navy mx-auto my-4 flex w-full max-w-[95vw] flex-col rounded-lg shadow-xl">
        {/* Header */}
        <div className="flex items-center justify-between border-b p-6">
          <div className="flex items-center gap-4">
            <h2 className="text-xl font-semibold">
              Visual Workflow Versions Comparison
            </h2>
          </div>
          <div className="flex items-center gap-4">
            <div className="flex gap-3 text-sm">
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
            <Button variant="ghost" size="icon" onClick={onClose}>
              <Cross1Icon className="h-4 w-4" />
            </Button>
          </div>
        </div>

        {/* Content */}
        <div className="flex flex-1 overflow-hidden">
          <div className="grid flex-1 grid-cols-2 gap-4 p-6">
            {/* Version 1 Column */}
            <ReactFlowProvider>
              <WorkflowComparisonRenderer
                key={`k1-${version1.workflow_id}v${version1.version}`}
                version={version1}
                title={`Version ${version1.version}`}
                blockColors={version1BlockColors}
              />
            </ReactFlowProvider>

            {/* Version 2 Column */}
            <ReactFlowProvider>
              <WorkflowComparisonRenderer
                key={`k2-${version2.workflow_id}v${version2.version}`}
                version={version2}
                title={`Version ${version2.version}`}
                blockColors={version2BlockColors}
              />
            </ReactFlowProvider>
          </div>
        </div>
      </div>
    </div>
  );
}

export { WorkflowVisualComparisonDrawer };
