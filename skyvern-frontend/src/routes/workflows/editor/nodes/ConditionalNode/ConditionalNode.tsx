import { useCallback, useEffect, useMemo, useRef } from "react";
import {
  Handle,
  NodeProps,
  Position,
  useNodes,
  useReactFlow,
  type Node,
} from "@xyflow/react";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";

import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { AppNode } from "..";
import { applyDescendantCollapseVisibility } from "../../collapse/applyDescendantCollapseVisibility";
import { scheduleCollapseRelayout } from "../../collapse/scheduleCollapseRelayout";
import { useCollapseContext } from "../../collapse/CollapseContext";
import {
  isBlockCollapsedAt,
  useIsBlockCollapsed,
  useNodeCollapseStore,
} from "../../collapse/useNodeCollapseStore";
import { getLoopNodeWidth } from "../../workflowEditorUtils";
import { type ConditionalNode } from "./types";
import { BranchesEditor } from "./BranchesEditor";

function ConditionalNodeComponent({ id, data }: NodeProps<ConditionalNode>) {
  const nodes = useNodes<AppNode>();
  const { updateNodeData, setNodes } = useReactFlow<AppNode>();
  const node = nodes.find((n) => n.id === id);
  const isCollapsed = useIsBlockCollapsed(data.label);
  const prevIsCollapsed = useRef<boolean | null>(null);
  const { open } = useCollapseContext();
  const workflowPermanentId = useWorkflowPermanentId();

  const children = useMemo(() => {
    return nodes.filter((node) => node.parentId === id && !node.hidden);
  }, [nodes, id]);

  const furthestDownChild: Node | null = useMemo(() => {
    return children.reduce(
      (acc, child) => {
        if (!acc) {
          return child;
        }
        if (
          child.position.y + (child.measured?.height ?? 0) >
          acc.position.y + (acc.measured?.height ?? 0)
        ) {
          return child;
        }
        return acc;
      },
      null as Node | null,
    );
  }, [children]);

  const childrenHeightExtent = useMemo(() => {
    return (
      (furthestDownChild?.measured?.height ?? 0) +
      (furthestDownChild?.position.y ?? 0) +
      24
    );
  }, [furthestDownChild]);

  const conditionalNodeWidth = useMemo(() => {
    return node ? getLoopNodeWidth(node, nodes) : 450;
  }, [node, nodes]);

  const observerRef = useRef<ResizeObserver | null>(null);
  const lastHeaderHeight = useRef<number | undefined>(data._headerHeight);

  // Callback ref re-runs on every mount/unmount of the inner card so the
  // observer always tracks the live element. A useEffect-captured ref
  // freezes on the original DOM node and goes stale once the subtree is
  // remounted (e.g., when the conditional becomes collapsible).
  const headerRef = useCallback(
    (el: HTMLDivElement | null) => {
      if (observerRef.current) {
        observerRef.current.disconnect();
        observerRef.current = null;
      }
      if (!el) return;
      lastHeaderHeight.current = undefined;
      const observer = new ResizeObserver(() => {
        const height = Math.round(el.offsetHeight);
        if (lastHeaderHeight.current !== height) {
          lastHeaderHeight.current = height;
          updateNodeData(id, { _headerHeight: height });
          window.dispatchEvent(new Event("conditional-header-resized"));
        }
      });
      observer.observe(el);
      observerRef.current = observer;
    },
    [id, updateNodeData],
  );

  // Hide/show all descendants when collapse state flips. Uses the shared
  // utility so a nested loop or conditional inside a branch also hides
  // recursively, and so the expand path respects inner blocks' own
  // collapse state in the zustand store.
  useEffect(() => {
    if (prevIsCollapsed.current === null && !isCollapsed) {
      prevIsCollapsed.current = false;
      return;
    }
    const previousIsCollapsed = prevIsCollapsed.current;
    prevIsCollapsed.current = isCollapsed;
    setNodes((prev) => {
      const collapsedSet = useNodeCollapseStore.getState().collapsed;
      const wpid = workflowPermanentId ?? "__global__";
      return applyDescendantCollapseVisibility(prev, id, isCollapsed, (label) =>
        isBlockCollapsedAt(collapsedSet, wpid, label),
      );
    });
    return scheduleCollapseRelayout(
      "conditional-header-resized",
      previousIsCollapsed,
      isCollapsed,
    );
  }, [id, isCollapsed, setNodes, workflowPermanentId]);

  if (!node) {
    // If the node has been removed or is not yet available, bail out gracefully.
    return null;
  }

  if (isCollapsed) {
    return (
      <div className="relative">
        <Handle
          type="target"
          position={Position.Top}
          id={`${id}-target`}
          className="opacity-0"
        />
        <div
          className={cn(
            "w-[30rem] rounded-lg bg-slate-elevation3 px-6 py-4 shadow-sm transition-shadow motion-reduce:transition-none",
            data.comparisonColor,
          )}
        >
          <NodeHeader
            blockLabel={data.label}
            editable={data.editable}
            nodeId={id}
            totpIdentifier={null}
            totpUrl={null}
            type="conditional"
          />
        </div>
        <Handle
          type="source"
          position={Position.Bottom}
          id={`${id}-source`}
          className="opacity-0"
        />
      </div>
    );
  }

  return (
    <div className="relative">
      <Handle
        type="target"
        position={Position.Top}
        id={`${id}-target`}
        className="opacity-0"
      />
      <div
        className="rounded-xl border-2 border-dashed border-border p-2 dark:border-slate-600"
        style={{
          width: conditionalNodeWidth,
          height: childrenHeightExtent,
        }}
      >
        <div className="flex w-full justify-center">
          <div
            ref={headerRef}
            className={cn(
              "w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4 transition-shadow motion-reduce:transition-none",
              open ? "shadow-md" : "shadow-sm",
              data.comparisonColor,
            )}
          >
            <NodeHeader
              blockLabel={data.label}
              editable={data.editable}
              nodeId={id}
              totpIdentifier={null}
              totpUrl={null}
              type="conditional"
            />
            <BranchesEditor nodeId={id} data={data} />
          </div>
        </div>
      </div>
      <Handle
        type="source"
        position={Position.Bottom}
        id={`${id}-source`}
        className="opacity-0"
      />
    </div>
  );
}

export { ConditionalNodeComponent as ConditionalNode };
