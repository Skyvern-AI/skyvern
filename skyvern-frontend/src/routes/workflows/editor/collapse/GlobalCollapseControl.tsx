import { ControlButton, useNodes } from "@xyflow/react";
import { DoubleArrowDownIcon, DoubleArrowUpIcon } from "@radix-ui/react-icons";
import { useMemo } from "react";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

import { useWorkflowScopeId } from "../WorkflowScopeContext";

import { collapsibleRfNodeTypes } from "./collapsibleBlockTypes";
import {
  isBlockCollapsedAt,
  useNodeCollapseStore,
} from "./useNodeCollapseStore";

// Standalone control rendered inside the React Flow `<Controls>` area so it
// inherits RF's provider context (`useNodes` is only valid inside a RF
// subtree). Toggles between collapsing every collapsible block and
// expanding all of them. Sentinels (`start`, `nodeAdder`) are excluded.
export function GlobalCollapseControl() {
  const nodes = useNodes();
  const workflowId = useWorkflowScopeId() ?? "__global__";
  const collapsed = useNodeCollapseStore((s) => s.collapsed);
  const collapseAll = useNodeCollapseStore((s) => s.collapseAll);
  const expandAll = useNodeCollapseStore((s) => s.expandAll);

  const collapsibleLabels = useMemo(() => {
    const labels: string[] = [];
    for (const node of nodes) {
      if (!node.type || !collapsibleRfNodeTypes.has(node.type)) continue;
      const label =
        typeof (node.data as { label?: unknown })?.label === "string"
          ? (node.data as { label: string }).label
          : "";
      if (label.length > 0) labels.push(label);
    }
    return labels;
  }, [nodes]);

  if (collapsibleLabels.length === 0) {
    return null;
  }

  // If every collapsible block is currently collapsed, the next press
  // expands all. Otherwise (some or none collapsed) the next press
  // collapses all — matching the "collapse everything with one click"
  // primary use case from Aaron's dogfood. NB: `[].every` returns true,
  // which would flip the icon to "expand all" for an empty workflow —
  // safe today only because the early return at line 44 short-circuits
  // before this runs.
  const allCollapsed = collapsibleLabels.every((l) =>
    isBlockCollapsedAt(collapsed, workflowId, l),
  );

  const handleClick = () => {
    if (allCollapsed) {
      expandAll(workflowId);
    } else {
      collapseAll(workflowId, collapsibleLabels);
    }
  };

  const label = allCollapsed ? "Expand all blocks" : "Collapse all blocks";
  const Icon = allCollapsed ? DoubleArrowDownIcon : DoubleArrowUpIcon;

  return (
    <TooltipProvider delayDuration={100}>
      <Tooltip>
        <TooltipTrigger asChild>
          <div>
            <ControlButton onClick={handleClick} aria-label={label}>
              <Icon />
            </ControlButton>
          </div>
        </TooltipTrigger>
        <TooltipContent side="right" className="z-[9999]">
          {label}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
