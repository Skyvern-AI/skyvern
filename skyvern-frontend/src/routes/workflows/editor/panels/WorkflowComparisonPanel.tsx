import { queryClient } from "@/api/QueryClient";
import { apiWorkflowToSettings } from "@/routes/workflows/editor/apiWorkflowToSettings";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type MutableRefObject,
  type ReactElement,
} from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  ChevronDownIcon,
  ChevronUpIcon,
  Cross2Icon,
} from "@radix-ui/react-icons";
import {
  ReactFlowProvider,
  useNodesState,
  useEdgesState,
  useReactFlow,
  useStore,
  getNodesBounds,
  NodeChange,
  EdgeChange,
  type Edge,
} from "@xyflow/react";
import { WorkflowVersion } from "../../hooks/useWorkflowVersionsQuery";
import { ControlTooltip } from "../../studio/ControlTooltip";
import { WorkflowBlock } from "../../types/workflowTypes";
import { FlowRenderer } from "../FlowRenderer";
import {
  getElements,
  workflowEffectiveDefaultEngine,
} from "../workflowEditorUtils";
import {
  PANE_FIT_DEBOUNCE_MS,
  revealNodeViewport,
  startAnchoredViewport,
} from "../paneFit";
import { AppNode, isWorkflowBlockNode } from "../nodes";
import { isWorkflowStartNodeData } from "../nodes/StartNode/types";
import type {
  BlockReviewAnnotation,
  FoldReviewAnnotation,
  StartReviewAnnotation,
} from "../review/reviewAnnotation";
import { ReviewStatusChip } from "../review/ReviewParts";
import {
  diffWorkflowVersions,
  foldUnchangedRuns,
  type BlockReview,
  type UnchangedFold,
  type WorkflowReviewDiff,
} from "./workflowReviewDiff";

type ComparisonMode = "history" | "copilot";

export type CopilotReviewStatus = "approve" | "reject" | "close";

type Props = {
  version1: WorkflowVersion;
  version2: WorkflowVersion;
  onSelectState?: (version: WorkflowVersion) => void;
  mode?: ComparisonMode;
  onCopilotReviewClose?: (status: CopilotReviewStatus) => void | Promise<void>;
  // Copilot mode: why Accept and Reject are held, e.g. while an Accept's outcome is unresolved.
  lockReason?: string | null;
  // History mode: exits comparison without applying a version (the studio's
  // "Keep current version"). Copilot mode keeps its own close flow.
  onExit?: () => void;
};

type CanvasElements = { nodes: Array<AppNode>; edges: Array<Edge> };

type BlockAnnotationLookup = (
  label: string,
) => BlockReviewAnnotation | FoldReviewAnnotation | undefined;

function annotatedElements(
  blocks: Array<WorkflowBlock>,
  version: WorkflowVersion,
  lookup: BlockAnnotationLookup,
  start?: StartReviewAnnotation,
): CanvasElements {
  // Deep clone so the canvas never shares objects with the editor's state.
  const { nodes, edges } = getElements(
    JSON.parse(JSON.stringify(blocks)),
    apiWorkflowToSettings(version),
    false,
    workflowEffectiveDefaultEngine(version, queryClient),
  );
  return {
    edges,
    nodes: nodes.map((node) => {
      if (node.type === "start") {
        return start && !node.parentId && isWorkflowStartNodeData(node.data)
          ? ({ ...node, data: { ...node.data, review: start } } as AppNode)
          : node;
      }
      if (!isWorkflowBlockNode(node)) return node;
      const review = lookup(node.data.label);
      return review
        ? ({ ...node, data: { ...node.data, review } } as AppNode)
        : node;
    }),
  };
}

function blockAnnotation(
  review: BlockReview | undefined,
  showStatus: boolean,
  withChanges: boolean,
): BlockReviewAnnotation | undefined {
  if (!review) return undefined;
  return {
    kind: "block",
    status: review.status,
    changes: withChanges ? review.changes : [],
    showStatus,
  };
}

type AnchorControl = MutableRefObject<{
  release: () => void;
  rearm: () => void;
  pane: () => DOMRect | undefined;
} | null>;

function ReviewFlowCanvas({
  elements,
  version,
  anchorControlRef,
}: {
  elements: CanvasElements;
  version: WorkflowVersion;
  // Lets the header stop or restart the start anchor and measure the pane.
  anchorControlRef?: AnchorControl;
}) {
  const reactFlow = useReactFlow<AppNode>();
  const [nodes, setNodes, onNodesChange] = useNodesState(elements.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(elements.edges);
  const containerRef = useRef<HTMLDivElement>(null);
  const userMovedRef = useRef(false);
  const anchorTimerRef = useRef<number | null>(null);

  // useNodesState only reads its initial argument; re-sync when the elements
  // change (Hide unchanged, a fold opened).
  useEffect(() => {
    setNodes(elements.nodes);
    setEdges(elements.edges);
  }, [elements, setNodes, setEdges]);

  // The review opens while its pane is still settling (the studio reveals the
  // Editor pane in the same step), so a one-shot fit lands off-center. Keep the
  // flow's start pinned to the top, re-applied after each layout pass and pane
  // resize, until the reviewer pans, zooms, or jumps to a change.
  const scheduleAnchor = useCallback(() => {
    if (userMovedRef.current) return;
    if (anchorTimerRef.current !== null) {
      window.clearTimeout(anchorTimerRef.current);
    }
    anchorTimerRef.current = window.setTimeout(() => {
      anchorTimerRef.current = null;
      const pane = containerRef.current?.getBoundingClientRect();
      const visible = reactFlow.getNodes().filter((node) => !node.hidden);
      if (userMovedRef.current || !pane || visible.length === 0) return;
      const viewport = startAnchoredViewport({
        pane: { width: pane.width, height: pane.height },
        bounds: getNodesBounds(visible),
      });
      if (viewport) void reactFlow.setViewport(viewport);
    }, PANE_FIT_DEBOUNCE_MS);
  }, [reactFlow]);

  useEffect(() => {
    scheduleAnchor();
  }, [nodes, scheduleAnchor]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const release = () => {
      userMovedRef.current = true;
    };
    if (anchorControlRef) {
      anchorControlRef.current = {
        release,
        rearm: () => {
          userMovedRef.current = false;
          scheduleAnchor();
        },
        pane: () => containerRef.current?.getBoundingClientRect(),
      };
    }
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(() => scheduleAnchor());
    observer?.observe(container);
    container.addEventListener("wheel", release, { passive: true });
    container.addEventListener("pointerdown", release);
    return () => {
      observer?.disconnect();
      container.removeEventListener("wheel", release);
      container.removeEventListener("pointerdown", release);
      if (anchorControlRef) anchorControlRef.current = null;
      if (anchorTimerRef.current !== null) {
        window.clearTimeout(anchorTimerRef.current);
        anchorTimerRef.current = null;
      }
    };
  }, [anchorControlRef, scheduleAnchor]);

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
    <div
      ref={containerRef}
      className="h-full w-full overflow-hidden rounded-lg border bg-background"
    >
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
  );
}

function pluralize(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function ReviewSummary({
  diff,
  lead,
}: {
  diff: WorkflowReviewDiff;
  lead: string;
}) {
  const { counts, inputs, settings } = diff;
  if (diff.isNewWorkflow) {
    return (
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-muted-foreground">New workflow</span>
        <ReviewStatusChip status="new" />
        <span className="text-muted-foreground">
          {pluralize(counts.new, "block")}
          {inputs.length > 0 ? `, ${pluralize(inputs.length, "input")}` : ""}
        </span>
      </div>
    );
  }
  const startParts = [
    inputs.length > 0 ? pluralize(inputs.length, "input") : null,
    settings.length > 0 ? pluralize(settings.length, "setting") : null,
  ].filter(Boolean);
  const hasBlockChanges = counts.new + counts.changed + counts.removed > 0;
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <span className="text-muted-foreground">{lead}</span>
      {counts.new > 0 ? (
        <ReviewStatusChip status="new" count={counts.new} />
      ) : null}
      {counts.changed > 0 ? (
        <ReviewStatusChip status="changed" count={counts.changed} />
      ) : null}
      {counts.removed > 0 ? (
        <ReviewStatusChip status="removed" count={counts.removed} />
      ) : null}
      {startParts.length > 0 ? (
        <span className="text-muted-foreground">+ {startParts.join(", ")}</span>
      ) : null}
      {!hasBlockChanges && startParts.length === 0 ? (
        <span className="text-muted-foreground">No changes</span>
      ) : null}
      <span className="text-muted-foreground">
        · {counts.unchanged} unchanged
      </span>
    </div>
  );
}

const START_TARGET = "__start_block__";

function CopilotReview({
  diff,
  version,
  isSettling,
  lockReason,
  canClose,
  onSettle,
}: {
  diff: WorkflowReviewDiff;
  version: WorkflowVersion;
  isSettling: boolean;
  lockReason: string | null;
  canClose: boolean;
  onSettle: (status: CopilotReviewStatus) => void;
}) {
  const reactFlow = useReactFlow<AppNode>();
  const hideUnchangedId = useId();
  const [hideUnchanged, setHideUnchanged] = useState(false);
  const [expandedFolds, setExpandedFolds] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const [cursor, setCursor] = useState(-1);
  const anchorControlRef: AnchorControl = useRef(null);
  const showStatus = !diff.isNewWorkflow;
  const withLockReason = (control: ReactElement) =>
    lockReason ? (
      <ControlTooltip
        content={<span className="block max-w-xs">{lockReason}</span>}
        blocked
      >
        {control}
      </ControlTooltip>
    ) : (
      control
    );
  const finallyLabel = version.workflow_definition?.finally_block_label ?? null;

  // Conditional levels never fold, so a workflow can have unchanged blocks and
  // still nothing to hide.
  const canFold = useMemo(
    () =>
      foldUnchangedRuns(diff.mergedBlocks, diff.merged, new Set(), finallyLabel)
        .folds.size > 0,
    [diff, finallyLabel],
  );

  const elements = useMemo(() => {
    const { blocks, folds } = hideUnchanged
      ? foldUnchangedRuns(
          diff.mergedBlocks,
          diff.merged,
          expandedFolds,
          finallyLabel,
        )
      : {
          blocks: diff.mergedBlocks,
          folds: new Map<string, UnchangedFold>(),
        };
    return annotatedElements(
      blocks,
      version,
      (label) => {
        const fold = folds.get(label);
        if (fold) {
          return {
            kind: "fold",
            count: fold.count,
            onShow: () =>
              setExpandedFolds((prev) => new Set(prev).add(fold.key)),
          };
        }
        return blockAnnotation(diff.merged.get(label), showStatus, true);
      },
      {
        kind: "start",
        inputs: diff.inputs,
        settings: diff.settings,
        showStatus,
      },
    );
  }, [diff, version, hideUnchanged, expandedFolds, finallyLabel, showStatus]);

  // A change inside a conditional's inactive branch has no card to jump to,
  // and the reviewer can switch branches, so visibility is read live.
  const hiddenLabels = useStore((store) =>
    store.nodes
      .flatMap((node) =>
        node.hidden && isWorkflowBlockNode(node as AppNode)
          ? [(node as AppNode).data.label as string]
          : [],
      )
      .join("\n"),
  );
  const targets = useMemo(() => {
    const hidden = new Set(hiddenLabels.split("\n"));
    return [
      ...(diff.inputs.length + diff.settings.length > 0 ? [START_TARGET] : []),
      ...diff.changeOrder.filter((label) => !hidden.has(label)),
    ];
  }, [diff, hiddenLabels]);
  useEffect(() => {
    setCursor(-1);
  }, [targets]);

  // React Flow's fitView queues a no-op node update that FlowRenderer's change
  // filter drops, so the jump sets the viewport itself, as FlowRenderer does.
  const focusChange = (index: number) => {
    const target = targets[index];
    if (target === undefined) return;
    setCursor(index);
    const node = reactFlow
      .getNodes()
      .find((candidate) =>
        target === START_TARGET
          ? candidate.type === "start" && !candidate.parentId
          : isWorkflowBlockNode(candidate) && candidate.data.label === target,
      );
    anchorControlRef.current?.release();
    const internal = node ? reactFlow.getInternalNode(node.id) : undefined;
    const pane = anchorControlRef.current?.pane();
    if (!internal || !pane) return;
    const viewport = revealNodeViewport({
      pane: { width: pane.width, height: pane.height },
      bounds: {
        ...internal.internals.positionAbsolute,
        width: internal.measured.width ?? 0,
        height: internal.measured.height ?? 0,
      },
    });
    if (viewport) void reactFlow.setViewport(viewport, { duration: 300 });
  };

  return (
    <div className="flex h-full w-full flex-col rounded-lg bg-slate-elevation2">
      <div className="flex flex-shrink-0 flex-wrap items-center gap-x-6 gap-y-3 p-4">
        <div className="min-w-0">
          <div className="text-xs font-medium uppercase tracking-[0.12em] text-muted-foreground">
            Copilot review
          </div>
          <h2 className="truncate text-lg font-semibold">{version.title}</h2>
        </div>
        <ReviewSummary diff={diff} lead="Copilot's changes" />
        <div className="ml-auto flex flex-wrap items-center gap-3">
          {targets.length > 0 && !diff.isNewWorkflow ? (
            <div className="flex items-center gap-1">
              <Button
                variant="ghost"
                size="icon"
                className="size-8"
                aria-label="Previous change"
                title="Previous change"
                onClick={() =>
                  focusChange(cursor <= 0 ? targets.length - 1 : cursor - 1)
                }
              >
                <ChevronUpIcon className="size-4" />
              </Button>
              <span
                aria-live="polite"
                className="min-w-[5.5rem] text-center text-xs tabular-nums text-muted-foreground"
              >
                {cursor >= 0
                  ? `Change ${cursor + 1} of ${targets.length}`
                  : pluralize(targets.length, "change")}
              </span>
              <Button
                variant="ghost"
                size="icon"
                className="size-8"
                aria-label="Next change"
                title="Next change"
                onClick={() => focusChange((cursor + 1) % targets.length)}
              >
                <ChevronDownIcon className="size-4" />
              </Button>
            </div>
          ) : null}
          {canFold ? (
            <div className="flex items-center gap-2">
              <Checkbox
                id={hideUnchangedId}
                checked={hideUnchanged}
                onCheckedChange={(checked) => {
                  setHideUnchanged(checked === true);
                  setExpandedFolds(new Set());
                  setCursor(-1);
                  anchorControlRef.current?.rearm();
                }}
              />
              <Label
                htmlFor={hideUnchangedId}
                className="cursor-pointer text-sm font-normal"
              >
                Hide unchanged
              </Label>
            </div>
          ) : null}
          {/* The panel renders outside the studio's provider in the classic editor. */}
          <TooltipProvider delayDuration={200}>
            {withLockReason(
              <Button
                size="sm"
                variant="secondary"
                onClick={() => onSettle("reject")}
                disabled={isSettling || Boolean(lockReason)}
                aria-label={
                  lockReason ? `Reject unavailable: ${lockReason}` : undefined
                }
                className="disabled:pointer-events-none"
              >
                Reject
              </Button>,
            )}
            {/* Matches the chat's Accept, so one decision looks the same in both places. */}
            {withLockReason(
              <Button
                size="sm"
                onClick={() => onSettle("approve")}
                disabled={isSettling || Boolean(lockReason)}
                aria-label={
                  lockReason
                    ? `Accept changes unavailable: ${lockReason}`
                    : undefined
                }
                className="bg-success text-success-foreground hover:bg-success/90 disabled:pointer-events-none"
              >
                Accept changes
              </Button>,
            )}
          </TooltipProvider>
          {canClose ? (
            <button
              type="button"
              onClick={() => onSettle("close")}
              disabled={isSettling}
              className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              title="Close (Esc)"
              aria-label="Close review"
            >
              <Cross2Icon className="h-5 w-5" />
            </button>
          ) : null}
        </div>
      </div>

      <Separator />

      <div className="relative flex-1 overflow-hidden p-4">
        {diff.isNewWorkflow ? (
          <div className="pointer-events-none absolute inset-x-0 top-8 z-10 flex justify-center px-8">
            <div className="flex items-center gap-2 rounded-lg border bg-slate-elevation3 px-3 py-2 text-sm shadow-md">
              <ReviewStatusChip status="new" />
              <span>
                This is a new workflow, so every block below is new. Nothing on
                the canvas is replaced.
              </span>
            </div>
          </div>
        ) : null}
        <ReviewFlowCanvas
          elements={elements}
          version={version}
          anchorControlRef={anchorControlRef}
        />
      </div>
    </div>
  );
}

function HistoryComparison({
  diff,
  version1,
  version2,
  onSelectState,
  onExit,
}: {
  diff: WorkflowReviewDiff;
  version1: WorkflowVersion;
  version2: WorkflowVersion;
  onSelectState?: (version: WorkflowVersion) => void;
  onExit?: () => void;
}) {
  const leftElements = useMemo(
    () =>
      annotatedElements(
        version1.workflow_definition?.blocks ?? [],
        version1,
        (label) => blockAnnotation(diff.before.get(label), true, false),
      ),
    [diff, version1],
  );
  const rightElements = useMemo(
    () =>
      annotatedElements(
        version2.workflow_definition?.blocks ?? [],
        version2,
        (label) => blockAnnotation(diff.after.get(label), true, true),
        {
          kind: "start",
          inputs: diff.inputs,
          settings: diff.settings,
          showStatus: true,
        },
      ),
    [diff, version2],
  );

  return (
    <div className="flex h-full w-full flex-col rounded-lg bg-slate-elevation2">
      <div className="flex-shrink-0 p-4 pb-3">
        {onExit && (
          <div className="mb-2 flex justify-end">
            <button
              type="button"
              onClick={onExit}
              title="Exit comparison (Esc)"
              className="inline-flex h-7 items-center rounded-md border border-border px-2 text-xs font-medium text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              Keep current version
            </button>
          </div>
        )}
        <div className="grid grid-cols-3 gap-4">
          <h2 className="text-center text-xl font-semibold">
            {version1.title}
          </h2>
          <h3 className="text-center text-lg font-medium text-muted-foreground">
            Version Comparison
          </h3>
          <h2 className="text-center text-xl font-semibold">
            {version2.title}
          </h2>

          <div className="text-center text-sm text-muted-foreground">
            [Version {version1.version}] •{" "}
            {new Date(version1.modified_at).toLocaleDateString()}
          </div>
          <div className="flex justify-center">
            <ReviewSummary diff={diff} lead="Changes" />
          </div>
          <div className="text-center text-sm text-muted-foreground">
            [Version {version2.version}] •{" "}
            {new Date(version2.modified_at).toLocaleDateString()}
          </div>

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
          <div />
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

      <div className="flex-1 overflow-hidden p-4">
        <div className="grid h-full grid-cols-2 gap-4">
          <ReactFlowProvider>
            <ReviewFlowCanvas
              key={`k1-${version1.workflow_id}v${version1.version}`}
              elements={leftElements}
              version={version1}
            />
          </ReactFlowProvider>
          <ReactFlowProvider>
            <ReviewFlowCanvas
              key={`k2-${version2.workflow_id}v${version2.version}`}
              elements={rightElements}
              version={version2}
            />
          </ReactFlowProvider>
        </div>
      </div>
    </div>
  );
}

function WorkflowComparisonPanel({
  version1,
  version2,
  onSelectState,
  mode = "history",
  onCopilotReviewClose,
  lockReason = null,
  onExit,
}: Props) {
  const diff = useMemo(
    () => diffWorkflowVersions(version1, version2),
    [version1, version2],
  );

  // Approve / Reject await a server round trip; a second click before it
  // settles would race a concurrent apply/clear against the same revision.
  const settlingRef = useRef(false);
  const [isSettling, setIsSettling] = useState(false);
  const settleCopilotReview = useCallback(
    async (status: CopilotReviewStatus) => {
      if (!onCopilotReviewClose || settlingRef.current) return;
      settlingRef.current = true;
      setIsSettling(true);
      try {
        await onCopilotReviewClose(status);
      } finally {
        settlingRef.current = false;
        setIsSettling(false);
      }
    },
    [onCopilotReviewClose],
  );

  // ESC key handler for copilot mode - close without rejecting
  useEffect(() => {
    if (mode !== "copilot" || !onCopilotReviewClose) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        void settleCopilotReview("close");
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [mode, onCopilotReviewClose, settleCopilotReview]);

  // ESC in history mode mirrors "Keep current version" when an exit is wired.
  useEffect(() => {
    if (mode !== "history" || !onExit) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onExit();
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [mode, onExit]);

  if (mode === "copilot") {
    return (
      <ReactFlowProvider>
        <CopilotReview
          diff={diff}
          version={version2}
          isSettling={isSettling}
          lockReason={lockReason}
          canClose={Boolean(onCopilotReviewClose)}
          onSettle={(status) => void settleCopilotReview(status)}
        />
      </ReactFlowProvider>
    );
  }

  return (
    <HistoryComparison
      diff={diff}
      version1={version1}
      version2={version2}
      onSelectState={onSelectState}
      onExit={onExit}
    />
  );
}

export { WorkflowComparisonPanel };
