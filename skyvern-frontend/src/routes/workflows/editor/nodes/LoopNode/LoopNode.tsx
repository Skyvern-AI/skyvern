import { useCallback, useEffect, useRef } from "react";
import {
  Handle,
  NodeProps,
  Position,
  useNodes,
  useReactFlow,
  type Node,
} from "@xyflow/react";
import { useParams } from "react-router-dom";

import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useRecordingStore } from "@/store/useRecordingStore";
import { cn } from "@/util/utils";

import { AppNode } from "..";
import { applyDescendantCollapseVisibility } from "../../collapse/applyDescendantCollapseVisibility";
import { useCollapseContext } from "../../collapse/CollapseContext";
import {
  isBlockCollapsedAt,
  useIsBlockCollapsed,
  useNodeCollapseStore,
} from "../../collapse/useNodeCollapseStore";
import type { WorkflowBlockType } from "@/routes/workflows/types/workflowTypes";
import { getLoopNodeWidth } from "../../workflowEditorUtils";
import { BuildModeOnly } from "../BuildModeOnly";
import { NodeHeader } from "../components/NodeHeader";
import { LoopEditor } from "./LoopEditor";
import type { LoopNode } from "./types";

function LoopNode({ id, data }: NodeProps<LoopNode>) {
  const nodes = useNodes<AppNode>();
  const node = nodes.find((n) => n.id === id);
  if (!node) {
    throw new Error("Node not found"); // not possible
  }
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel, workflowPermanentId } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;
  const children = nodes.filter((node) => node.parentId === id);
  const recordingStore = useRecordingStore();
  const observerRef = useRef<ResizeObserver | null>(null);
  const { updateNodeData, setNodes } = useReactFlow<AppNode>();
  const lastHeaderHeight = useRef<number | undefined>(data._headerHeight);
  const isCollapsed = useIsBlockCollapsed(label);
  const prevIsCollapsed = useRef<boolean | null>(null);
  const { open } = useCollapseContext();

  // Callback ref re-runs on every mount/unmount of the inner card, which is
  // exactly when collapse/expand swaps the JSX subtree. A useEffect-captured
  // ref would freeze on the original element and stop firing after the first
  // collapse, causing _headerHeight to drift across cycles.
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
          window.dispatchEvent(new Event("loop-header-resized"));
        }
      });
      observer.observe(el);
      observerRef.current = observer;
    },
    [id, updateNodeData],
  );

  // Hide/show all descendants when collapse state flips. Walks recursively
  // so a nested conditional's branch contents (whose parentId is the
  // conditional, not this loop) also hide. On expand, respects any inner
  // collapsed block in the zustand store so a previously-collapsed inner
  // block stays collapsed. Skip on initial mount when already expanded to
  // avoid creating new node objects before the ResizeObserver fires (which
  // would reset RF's `measured` and cause marginy to fall back to the
  // 225px default, producing a large gap above the start block).
  useEffect(() => {
    if (prevIsCollapsed.current === null && !isCollapsed) {
      prevIsCollapsed.current = false;
      return;
    }
    const wasCollapsed = prevIsCollapsed.current === true;
    prevIsCollapsed.current = isCollapsed;
    setNodes((prev) => {
      const collapsedSet = useNodeCollapseStore.getState().collapsed;
      const wpid = workflowPermanentId ?? "__global__";
      return applyDescendantCollapseVisibility(prev, id, isCollapsed, (label) =>
        isBlockCollapsedAt(collapsedSet, wpid, label),
      );
    });
    // After expanding, fire a re-layout so children land at the correct
    // marginy even if debouncedLayoutForDimensions already ran with stale
    // data before the header's ResizeObserver had a chance to update
    // _headerHeight. The handler has a built-in 10ms delay that lets the
    // ResizeObserver win the race.
    if (wasCollapsed && !isCollapsed) {
      window.dispatchEvent(new Event("loop-header-resized"));
    }
  }, [id, isCollapsed, setNodes, workflowPermanentId]);

  const furthestDownChild: Node | null = children.reduce(
    (acc, child) => {
      if (!acc) {
        return child;
      }
      if (child.position.y > acc.position.y) {
        return child;
      }
      return acc;
    },
    null as Node | null,
  );

  const childrenHeightExtent =
    (furthestDownChild?.measured?.height ?? 0) +
    (furthestDownChild?.position.y ?? 0) +
    24;

  const loopNodeWidth = getLoopNodeWidth(node, nodes);
  const loopKind = data.loopKind;
  const headerBlockType: WorkflowBlockType =
    loopKind === "while" ? "while_loop" : "for_loop";

  if (isCollapsed) {
    return (
      <div
        className={cn({
          "pointer-events-none opacity-50": recordingStore.isRecording,
        })}
      >
        <Handle
          type="source"
          position={Position.Bottom}
          id="a"
          className="opacity-0"
        />
        <Handle
          type="target"
          position={Position.Top}
          id="b"
          className="opacity-0"
        />
        <div
          className={cn(
            "w-[30rem] rounded-lg bg-slate-elevation3 px-6 py-4 shadow-sm transition-all motion-reduce:transition-none",
            {
              "pointer-events-none": thisBlockIsPlaying,
              "bg-slate-950 outline outline-2 outline-slate-300":
                thisBlockIsTargetted,
            },
            data.comparisonColor,
          )}
        >
          <NodeHeader
            blockLabel={label}
            editable={editable}
            nodeId={id}
            totpIdentifier={null}
            totpUrl={null}
            type={headerBlockType}
          />
        </div>
      </div>
    );
  }

  return (
    <div
      className={cn({
        "pointer-events-none opacity-50": recordingStore.isRecording,
      })}
    >
      <Handle
        type="source"
        position={Position.Bottom}
        id="a"
        className="opacity-0"
      />
      <Handle
        type="target"
        position={Position.Top}
        id="b"
        className="opacity-0"
      />
      <div
        className="rounded-xl border-2 border-dashed border-slate-600 p-2"
        style={{
          width: loopNodeWidth,
          height: childrenHeightExtent,
        }}
      >
        <div className="flex w-full justify-center">
          <div
            ref={headerRef}
            className={cn(
              "transform-origin-center w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4 transition-all motion-reduce:transition-none",
              open ? "shadow-md" : "shadow-sm",
              {
                "pointer-events-none": thisBlockIsPlaying,
                "bg-slate-950 outline outline-2 outline-slate-300":
                  thisBlockIsTargetted,
              },
              data.comparisonColor,
            )}
          >
            <NodeHeader
              blockLabel={label}
              editable={editable}
              nodeId={id}
              totpIdentifier={null}
              totpUrl={null}
              type={headerBlockType}
            />
            <BuildModeOnly>
              <LoopEditor blockId={id} />
            </BuildModeOnly>
          </div>
        </div>
      </div>
    </div>
  );
}

export { LoopNode };
